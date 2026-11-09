import { mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import nunjucks from 'nunjucks';

const renderer = new nunjucks.Environment(null, {
  autoescape: false, throwOnUndefined: true,
  tags: { variableStart: '${{', variableEnd: '}}' },
});

export function renderTree(source, destination, values, skipTemplating = false) {
  mkdirSync(destination, { recursive: true });
  for (const entry of readdirSync(source, { withFileTypes: true })) {
    const src = join(source, entry.name);
    const dest = join(destination, entry.name);
    const skip = skipTemplating || ['.github', 'scripts'].includes(entry.name);
    if (entry.isDirectory()) renderTree(src, dest, values, skip);
    else {
      const contents = readFileSync(src, 'utf8');
      writeFileSync(dest, skip ? contents : renderer.renderString(contents, { values }));
    }
  }
}

if (process.argv[1].endsWith('/render.mjs')) {
  const destination = process.argv[2];
  if (!destination) throw new Error('Usage: node tests/scaffolding/render.mjs OUTPUT_DIRECTORY');
  renderTree('backstage/templates/microservice/skeleton', destination, {
    name: 'sample-api', description: 'Example service', owner: 'group:default/platform-team',
    repoOwner: 'example-org', repoName: 'sample-api',
  });
}
