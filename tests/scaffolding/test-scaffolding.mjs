import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { after, test } from 'node:test';
import YAML from 'yaml';
import { renderTree } from './render.mjs';

const repository = resolve(import.meta.dirname, '../..');
const temporary = mkdtempSync(join(tmpdir(), 'platform-scaffold-'));
after(() => rmSync(temporary, { recursive: true, force: true }));
const values = {
  name: 'catalog-api', description: 'A "quoted" description: with newline\nand Unicode — valid',
  owner: 'group:default/platform-team', repoOwner: 'Example-Org', repoName: 'Catalog-Service',
};
const service = join(temporary, 'service');
const registration = join(temporary, 'registration');
renderTree(join(repository, 'backstage/templates/microservice/skeleton'), service, values);
renderTree(join(repository, 'backstage/templates/microservice/registration'), registration, values);
const read = file => readFileSync(join(service, file), 'utf8');
const parse = file => YAML.parse(read(file));

for (const environment of ['dev', 'prod']) {
  test(`${environment}: generated manifests render, isolate traffic and satisfy platform policy`, () => {
    const rendered = execFileSync('kustomize', ['build', join(service, 'k8s/overlays', environment)], { encoding: 'utf8' });
    const objects = YAML.parseAllDocuments(rendered).map(document => document.toJSON());
    const deployment = objects.find(object => object.kind === 'Deployment');
    assert.equal(deployment.spec.replicas, 0);
    assert.equal(deployment.spec.template.spec.serviceAccountName, values.name);
    const container = deployment.spec.template.spec.containers[0];
    assert.match(container.image, /^ghcr\.io\/example-org\/catalog-service@sha256:[a-f0-9]{64}$/);
    assert.equal(container.securityContext.readOnlyRootFilesystem, true);
    const policy = objects.find(object => object.kind === 'NetworkPolicy');
    assert.equal(policy.spec.podSelector.matchLabels.app, deployment.spec.template.metadata.labels.app);
    assert.deepEqual(policy.spec.policyTypes, ['Ingress', 'Egress']);
    assert.equal(policy.spec.egress.length, 1);
    const route = objects.find(object => object.kind === 'HTTPRoute');
    assert.equal(route.spec.parentRefs[0].namespace, 'envoy-gateway-system');
    execFileSync('conftest', ['test', '--combine', '--policy', join(repository, 'policy'), '-'], { input: rendered });
  });
}

test('registration limits delivery to reviewed repository and resource kinds', () => {
  const [project, applicationSet] = YAML.parseAllDocuments(readFileSync(join(registration, 'applicationset.yaml'), 'utf8')).map(document => document.toJSON());
  assert.deepEqual(project.spec.sourceRepos, ['https://github.com/Example-Org/Catalog-Service.git']);
  assert.deepEqual(project.spec.destinations, [{ server: 'https://kubernetes.default.svc', namespace: 'apps' }]);
  assert.deepEqual(project.spec.clusterResourceWhitelist, []);
  assert(!project.spec.namespaceResourceWhitelist.some(resource => ['*', 'Secret', 'RoleBinding', 'Role'].includes(resource.kind)));
  assert.equal(applicationSet.spec.template.spec.project, project.metadata.name);
  assert.deepEqual(applicationSet.spec.generators[0].clusters.selector.matchExpressions[0].values, ['dev', 'prod']);
});

test('app registrations keep retrying late operator CRDs without pinning a failed revision', () => {
  const apps = join(repository, 'kubernetes/apps');
  const registrations = readdirSync(apps, { withFileTypes: true })
    .filter(entry => entry.isDirectory())
    .map(entry => join(apps, entry.name, 'applicationset.yaml'));
  assert.equal(registrations.length, 8, 'cover every platform app registration');
  registrations.push(join(registration, 'applicationset.yaml'));
  for (const path of registrations) {
    const applicationSet = YAML.parseAllDocuments(readFileSync(path, 'utf8'))
      .map(document => document.toJSON()).find(object => object.kind === 'ApplicationSet');
    // Check the generated child's policy, not the ApplicationSet controller policy.
    // A finite retry budget caused cold installs to remain failed after KEDA was ready.
    const policy = applicationSet.spec.template.spec.syncPolicy;
    assert.deepEqual(policy.automated, { prune: true, selfHeal: true }, path);
    assert.equal(policy.retry?.limit, -1, `${path}: late CRDs must not exhaust retries`);
    assert.equal(policy.retry.refresh, true, `${path}: retries must pick up a corrected revision`);
    assert.deepEqual(policy.retry.backoff, { duration: '10s', factor: 2, maxDuration: '60s' }, path);
  }
});

test('workflow expressions survive rendering and privileged release is gated', () => {
  const workflow = parse('.github/workflows/delivery.yaml');
  assert.equal(workflow.jobs.release.needs, 'test');
  assert.equal(workflow.jobs.promotion.needs, 'release');
  assert.match(workflow.jobs.release.if, /github.ref == 'refs\/heads\/main'/);
  assert.match(workflow.jobs.promotion.environment, /production/);
  assert.deepEqual(workflow.permissions, {});
  assert.equal(workflow.jobs.test.permissions['contents'], 'read');
  const releaseSteps = workflow.jobs.release.steps;
  assert(releaseSteps.findIndex(step => step.name === 'Sign only after both variants pass') > releaseSteps.findIndex(step => step.name === 'Scan published variants by immutable digest'));
  const promotion = workflow.jobs.promotion.steps.at(-1).run;
  assert(promotion.indexOf('cosign verify') < promotion.indexOf('python3 scripts/promote.py'));
  assert.match(promotion, /--certificate-identity "https:\/\/github.com\/\$GITHUB_REPOSITORY\/\.github\/workflows\/delivery.yaml@refs\/heads\/main"/);
  for (const job of Object.values(workflow.jobs)) for (const step of job.steps) {
    if (step.uses && !step.uses.startsWith('./')) assert.match(step.uses, /@[a-f0-9]{40}$/);
    if (step.run) {
      // GitHub expressions are env/with fields, never executable interpolation.
      assert(!step.run.includes('${{'));
      execFileSync('bash', ['-n'], { input: step.run });
    }
  }
});

test('promotion changes only chosen environment and rejects untrusted input', () => {
  const originalProd = read('k8s/overlays/prod/kustomization.yaml');
  const script = join(service, 'scripts/promote.py');
  const env = { ...process.env, GITHUB_REPOSITORY: 'Example-Org/Catalog-Service', IMAGE: 'ghcr.io/example-org/catalog-service', DIGEST: `sha256:${'a'.repeat(64)}`, TARGET_ENVIRONMENT: 'dev' };
  execFileSync('python3', [script], { cwd: service, env });
  assert.equal(parse('k8s/overlays/dev/kustomization.yaml').replicas[0].count, 1);
  assert.equal(parse('k8s/overlays/dev/kustomization.yaml').images[0].digest, env.DIGEST);
  assert.equal(read('k8s/overlays/prod/kustomization.yaml'), originalProd);
  for (const invalid of [{ TARGET_ENVIRONMENT: '../../base' }, { IMAGE: 'ghcr.io/attacker/service' }, { DIGEST: 'latest' }]) {
    assert.notEqual(spawnSync('python3', [script], { cwd: service, env: { ...env, ...invalid } }).status, 0);
  }
  execFileSync('python3', [script], { cwd: service, env: { ...env, TARGET_ENVIRONMENT: 'prod' } });
  assert.equal(parse('k8s/overlays/prod/kustomization.yaml').replicas[0].count, 2);
});

test('generated service uses a consistent frozen lock and passes endpoint/metrics tests', () => {
  execFileSync('uv', ['lock', '--check'], { cwd: service, stdio: 'pipe' });
  execFileSync('uv', ['run', '--frozen', 'pytest', '-q'], { cwd: service, env: { ...process.env, OTEL_SDK_DISABLED: 'true' }, stdio: 'pipe' });
});

test('catalog uses actual repository coordinates and preserves user description', () => {
  const catalog = parse('catalog-info.yaml');
  assert.equal(catalog.metadata.annotations['github.com/project-slug'], 'Example-Org/Catalog-Service');
  assert.equal(catalog.metadata.description, values.description);
});

test('Backstage publishes service and sends only reviewed registration files to platform', () => {
  const template = YAML.parse(readFileSync(join(repository, 'backstage/templates/microservice/template.yaml'), 'utf8'));
  const steps = template.spec.steps;
  assert.equal(steps.find(step => step.id === 'fetch').input.targetPath, 'service');
  assert.deepEqual(steps.find(step => step.id === 'fetch').input.copyWithoutTemplating, ['.github/workflows/**', '.github/actions/**', 'scripts/**']);
  assert.equal(steps.find(step => step.id === 'publish').input.sourcePath, 'service');
  const registrationPR = steps.find(step => step.id === 'registration-pr');
  assert.equal(registrationPR.action, 'publish:github:pull-request');
  assert.equal(registrationPR.input.sourcePath, 'registration');
  assert.equal(registrationPR.input.targetPath, 'kubernetes/apps/${{ parameters.name }}');
  assert(!JSON.stringify(template.spec.parameters).includes('namespace'));
});

test('secondary templates keep repository coordinates and deployment claims explicit', () => {
  for (const kind of ['platform-app', 'llm-service']) {
    const output = join(temporary, kind);
    renderTree(join(repository, 'backstage/templates', kind, 'skeleton'), output, {
      ...values, model: 'model-name', imageRepository: 'registry.example.com/application',
      imageTag: 'v1.2.3', namespace: 'apps',
    });
    const catalog = YAML.parse(readFileSync(join(output, 'catalog-info.yaml'), 'utf8'));
    assert.equal(catalog.metadata.annotations['github.com/project-slug'], 'Example-Org/Catalog-Service');
    if (kind === 'platform-app') {
      const app = YAML.parse(readFileSync(join(output, 'application.yaml'), 'utf8'));
      assert.equal(app.spec.project, 'platform-apps');
      assert.equal(app.spec.sources[1].repoURL, 'https://github.com/Example-Org/Catalog-Service.git');
      const template = YAML.parse(readFileSync(join(repository, 'backstage/templates', kind, 'template.yaml'), 'utf8'));
      const tag = template.spec.parameters[0].properties.imageTag;
      assert(!new RegExp(tag.pattern).test('latest'));
      assert(new RegExp(tag.pattern).test('v1.2.3'));
    } else {
      const main = readFileSync(join(output, 'main.py'), 'utf8');
      assert(main.includes('os.environ["LLM_GATEWAY_API_KEY"]'));
      assert(main.includes('http://llm-gateway.apps.svc/v1'));
      assert(!main.includes('prompt=prompt'));
    }
  }
});

test('Backstage route gives remote template fetches a bounded request budget', () => {
  const chart = join(repository, 'helm/backstage');
  for (const environment of ['dev', 'prod']) {
    const output = execFileSync('helm', ['template', 'backstage', chart, '-f', join(chart, `values-${environment}.yaml`)], { encoding: 'utf8' });
    const objects = YAML.parseAllDocuments(output).map(document => document.toJSON());
    assert.equal(objects.find(object => object.kind === 'HTTPRoute').spec.rules[0].timeouts.request, '120s');
  }
  for (const timeout of ['0s', '-1s', 'unlimited']) {
    const result = spawnSync('helm', ['template', 'backstage', chart, '--set-string', `httpRoute.requestTimeout=${timeout}`], { encoding: 'utf8' });
    assert.notEqual(result.status, 0, `${timeout} must not disable the request bound`);
  }
});

test('Backstage guest authentication is dev-only and production fails closed', () => {
  const chart = join(repository, 'helm/backstage');
  const configFor = environment => {
    const output = execFileSync('helm', ['template', 'backstage', chart, '-f', join(chart, `values-${environment}.yaml`)], { encoding: 'utf8' });
    const objects = YAML.parseAllDocuments(output).map(document => document.toJSON());
    return YAML.parse(objects.find(object => object.kind === 'ConfigMap').data['app-config.local.yaml']);
  };
  assert.equal(configFor('dev').auth.providers.guest.dangerouslyAllowOutsideDevelopment, true);
  assert.equal(configFor('prod').auth.providers.guest.dangerouslyAllowOutsideDevelopment, false);
  const bad = spawnSync('helm', ['template', 'backstage', chart, '--set', 'environment=prod,auth.guest.enabled=true'], { encoding: 'utf8' });
  assert.notEqual(bad.status, 0);
  assert.match(bad.stderr, /guest authentication is permitted only with environment=dev/);
  const defaults = YAML.parseAllDocuments(execFileSync('helm', ['template', 'backstage', chart], { encoding: 'utf8' })).map(document => document.toJSON());
  const config = YAML.parse(defaults.find(object => object.kind === 'ConfigMap').data['app-config.local.yaml']);
  assert.equal(config.auth.providers.guest.dangerouslyAllowOutsideDevelopment, false);
  assert.equal(config.argocd, undefined);
  assert.equal(config.grafana, undefined);
});

test('Backstage config changes roll pods and GitHub credentials come only from Secret references', () => {
  const chart = join(repository, 'helm/backstage');
  const render = args => YAML.parseAllDocuments(execFileSync('helm', ['template', 'backstage', chart, ...args], { encoding: 'utf8' })).map(document => document.toJSON());
  const baseline = render([]);
  const configured = render(['-f', join(chart, 'values-dev.yaml'), '--set', 'github.tokenSecret=reviewed-github-secret']);
  const pod = configured.find(object => object.kind === 'Deployment').spec.template;
  assert.notEqual(pod.metadata.annotations['checksum/config'], baseline.find(object => object.kind === 'Deployment').spec.template.metadata.annotations['checksum/config']);
  const token = pod.spec.containers[0].env.find(variable => variable.name === 'GITHUB_TOKEN');
  assert.deepEqual(token.valueFrom.secretKeyRef, { name: 'reviewed-github-secret', key: 'token' });
  const config = YAML.parse(configured.find(object => object.kind === 'ConfigMap').data['app-config.local.yaml']);
  assert.equal(config.integrations.github[0].token, '${GITHUB_TOKEN}');
  const applicationSet = YAML.parse(readFileSync(join(repository, 'kubernetes/platform/backstage/applicationset.yaml'), 'utf8'));
  assert.deepEqual(applicationSet.spec.generators[0].clusters.selector.matchExpressions[0].values, ['dev', 'prod']);
  assert(applicationSet.spec.template.spec.source.helm.valueFiles.includes('values-{{.metadata.labels.environment}}.yaml'));
});

test('portal catalog selects real workload labels in the correct namespace', () => {
  const entities = YAML.parseAllDocuments(readFileSync(join(repository, 'helm/backstage/files/catalog/components.yaml'), 'utf8')).map(document => document.toJSON());
  for (const entity of entities) {
    const annotations = entity.metadata.annotations;
    const name = entity.metadata.name;
    if (name === 'platform-kafka') {
      assert.equal(annotations['backstage.io/kubernetes-label-selector'], 'strimzi.io/cluster=platform-kafka');
      assert.equal(annotations['backstage.io/kubernetes-namespace'], 'platform');
    } else {
      const resources = YAML.parseAllDocuments(execFileSync('kustomize', ['build', join(repository, `services/${name}/k8s/overlays/dev`)], {encoding:'utf8'})).map(document=>document.toJSON());
      const workload = resources.find(resource => ['Deployment','Rollout','StatefulSet'].includes(resource.kind) && resource.metadata.name === name);
      assert(workload, `${name} workload must exist`);
      assert.equal(annotations['backstage.io/kubernetes-label-selector'], `app=${workload.spec.template.metadata.labels.app}`);
      assert.equal(annotations['backstage.io/kubernetes-namespace'], workload.metadata.namespace);
    }
  }
});

test('Backstage skips unavailable metrics API and scopes optional metrics permissions', () => {
  const chart = join(repository, 'helm/backstage');
  for (const enabled of [false, true]) {
    const rendered = execFileSync('helm', ['template', 'backstage', chart, '--set', `kubernetes.skipMetricsLookup=${!enabled}`], {encoding:'utf8'});
    const objects = YAML.parseAllDocuments(rendered).map(document=>document.toJSON());
    const config = YAML.parse(objects.find(object=>object.kind==='ConfigMap').data['app-config.local.yaml']);
    assert.equal(config.kubernetes.clusterLocatorMethods[0].clusters[0].skipMetricsLookup, !enabled);
    const rules = objects.find(object=>object.kind==='ClusterRole').rules;
    const metrics = rules.find(rule=>rule.apiGroups.includes('metrics.k8s.io'));
    if (enabled) assert.deepEqual(metrics, {apiGroups:['metrics.k8s.io'],resources:['pods'],verbs:['get','list']});
    else assert.equal(metrics, undefined);
  }
});
