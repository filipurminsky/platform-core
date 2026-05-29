package main

# Every Deployment must have resource requests and limits
deny[msg] {
  input.kind == "Deployment"
  container := input.spec.template.spec.containers[_]
  not container.resources.requests
  msg := sprintf("Deployment '%s': container '%s' is missing resource requests", [input.metadata.name, container.name])
}

deny[msg] {
  input.kind == "Deployment"
  container := input.spec.template.spec.containers[_]
  not container.resources.limits
  msg := sprintf("Deployment '%s': container '%s' is missing resource limits", [input.metadata.name, container.name])
}

# Every Deployment must have a liveness probe
deny[msg] {
  input.kind == "Deployment"
  container := input.spec.template.spec.containers[_]
  not container.livenessProbe
  msg := sprintf("Deployment '%s': container '%s' is missing a liveness probe", [input.metadata.name, container.name])
}

# Every Deployment must have a readiness probe
deny[msg] {
  input.kind == "Deployment"
  container := input.spec.template.spec.containers[_]
  not container.readinessProbe
  msg := sprintf("Deployment '%s': container '%s' is missing a readiness probe", [input.metadata.name, container.name])
}

# Containers must not run as root
deny[msg] {
  input.kind == "Deployment"
  container := input.spec.template.spec.containers[_]
  container.securityContext.runAsUser == 0
  msg := sprintf("Deployment '%s': container '%s' must not run as root (runAsUser: 0)", [input.metadata.name, container.name])
}

# Deployments should have the app.kubernetes.io/part-of label
warn[msg] {
  input.kind == "Deployment"
  not input.metadata.labels["app.kubernetes.io/part-of"]
  msg := sprintf("Deployment '%s' is missing the 'app.kubernetes.io/part-of' label", [input.metadata.name])
}
