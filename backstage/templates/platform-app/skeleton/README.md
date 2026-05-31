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

Apply `application.yaml` to the cluster (or let the platform App-of-Apps pick it
up). ArgoCD renders `helm/demo-app` from the platform-core repo with the
`values.yaml` in this repo, then keeps the deployment in sync.
