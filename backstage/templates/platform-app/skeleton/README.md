# ${{ values.name }}

${{ values.description }}

GitOps delivery for image `${{ values.imageRepository }}:${{ values.imageTag }}`,
scaffolded from the **New Platform App** template.

## What's here

| File              | Purpose                                            |
|-------------------|----------------------------------------------------|
| `values.yaml`     | demo-app Helm values (image, replicas, resources)  |
| `application.yaml`| ArgoCD Application — multi-source (chart + values)  |
| `catalog-info.yaml` | Backstage catalog registration                   |

## Delivery

A platform owner must review this Application, explicitly authorize this source
repository in the `platform-apps` AppProject, and add the Application under
`kubernetes/apps/<name>/application.yaml` through a platform PR. Publishing this
repository alone does not register it. ArgoCD renders `helm/demo-app` from the platform-core repo with the
`values.yaml` in this repo, then keeps the deployment in sync.
