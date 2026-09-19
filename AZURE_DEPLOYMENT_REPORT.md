# Azure Deployment Report

**Deployment date:** 19 September 2026  
**Azure region:** `centralindia`  
**Subscription:** `Azure subscription 1`

## Live application

- Application: https://cfai-product-catalog.politedesert-8cf38796.centralindia.azurecontainerapps.io/
- Health check: https://cfai-product-catalog.politedesert-8cf38796.centralindia.azurecontainerapps.io/health
- Swagger UI: https://cfai-product-catalog.politedesert-8cf38796.centralindia.azurecontainerapps.io/docs

The health endpoint returned **HTTP 200** after deployment.

## Deployment outcome

| Check | Result | Evidence / outcome |
|---|---|---|
| Azure authentication | Succeeded | The selected subscription is enabled. |
| Resource group | Succeeded | `cfai-product-catalog-rg` was created. |
| Container Apps environment | Succeeded | `cfai-product-catalog-env` provisioned successfully. |
| Container registry | Succeeded | Basic registry `cfaipcat265c150a` was created. |
| Image | Succeeded | Registry contains `cfai-product-catalog:v1`. |
| Container App revision | Succeeded | Active revision `cfai-product-catalog--blpg2gj` is provisioned with one replica. |
| External health check | Succeeded | `GET /health` returned HTTP 200. |

## Issues encountered and resolution

| Issue | Resolution |
|---|---|
| `az` was not on the initial terminal `PATH`. | Azure CLI was invoked from its Windows installation path; open a new terminal after installation to use `az` normally. |
| The first signed-in account had no visible subscription and MFA was required. | Device-code/MFA sign-in was completed and the enabled subscription was selected. |
| `Microsoft.App`, `Microsoft.ContainerRegistry`, and `Microsoft.OperationalInsights` were not registered. | The required resource providers were registered before provisioning continued. |
| The managed environment initially reported `Waiting`. | Azure completed its asynchronous environment and Log Analytics workspace provisioning; final state was `Succeeded`. |
| ACR source packaging included the local `env/` folder (about 1.2 GB), causing unnecessary queued build attempts. | A minimal temporary build context was submitted. `.dockerignore` was added to prevent local environments from entering Docker build contexts. Obsolete queued quick-build runs were cancelled. |
| Local Docker was unavailable. | The image was built with `az acr build` in Azure Container Registry, so no local Docker daemon was required. |

## Azure services used

| Service | Deployed resource | Purpose | Cost behaviour |
|---|---|---|---|
| Azure Resource Manager | `cfai-product-catalog-rg` | Logical resource group for all deployment resources. | No standalone charge. |
| Azure Container Apps (Consumption) | `cfai-product-catalog` | Runs the FastAPI container with public HTTPS ingress on port 8080. Configured for 1 vCPU, 2 GiB memory, minimum 1 and maximum 5 replicas. | Compute, memory, and requests are billed by use; the minimum replica can incur idle charges. |
| Azure Container Apps managed environment | `cfai-product-catalog-env` | Shared hosting environment, networking, revisions, and log connection. | No separate dedicated-plan charge was configured; associated log ingestion may cost money. |
| Azure Container Registry, Basic SKU | `cfaipcat265c150a` | Stores `cfai-product-catalog:v1` and performed cloud image builds. | Basic tier daily registry charge, included storage allowance, potential extra storage, and per-second ACR Tasks build charges. |
| Azure Monitor / Log Analytics | `workspace-cfaiproductcatalogrgN31b` | Receives Container Apps platform and application logs. | Log ingestion, extended retention, alerts, and data export can incur charges. |

No database, Key Vault, Storage Account, Application Gateway, CDN, virtual network,
or static public IP was created by this deployment.

## Cost baseline and controls

Actual billed cost is not available immediately after deployment because Azure cost
data is delayed and depends on your subscription offer and currency. Review **Cost
Management + Billing** in the Azure portal after 24--48 hours for the authoritative
amount. Do not treat an estimate as an invoice.

The current always-on baseline is one replica × 1 vCPU × 2 GiB. For a 730-hour
month, that represents up to **730 vCPU-hours** and **1,460 GiB-hours** before
Azure's idle-rate rules, free grant, traffic, and scale-out are applied. The
Container Apps consumption grant includes 180,000 vCPU-seconds (50 vCPU-hours),
360,000 GiB-seconds (100 GiB-hours), and two million requests per subscription per
month. Charges apply after those grants; inactive minimum replicas use a reduced
idle rate, while an app scaled to zero has no usage charge.

| Cost source | Current configuration | How to control it |
|---|---|---|
| Container Apps compute and memory | Minimum replica is `1`; maximum is `5`. | For non-production use, set `--min-replicas 0` to remove idle compute charges, accepting cold starts. Keep max replicas low until load testing proves a higher limit is needed. |
| HTTP requests | Public API and `/health` endpoint. | The first 2 million requests/month are included in the consumption grant; protect public endpoints and avoid overly frequent external health probes. |
| ACR Basic registry | Registry is retained and uses the Basic tier. | Delete unused image tags and delete the registry when the deployment is no longer needed. Basic includes 10 GB of storage; storage over that allowance is charged. |
| ACR Tasks builds | One successful image build; duplicate build runs were cancelled. | Build only immutable tags needed for releases; remove stale images and avoid uploading local virtual environments. |
| Log Analytics | Workspace was auto-created during environment provisioning. | Set a daily ingestion cap, reduce verbose logs, and avoid extended retention/export unless required. |
| Internet data transfer | Public HTTPS ingress. | Charges, if any, depend on outbound data volume and destination; keep large downloads out of the API. |

Current pricing references:

- [Azure Container Apps pricing](https://azure.microsoft.com/en-us/pricing/details/container-apps/)
- [Azure Container Registry pricing](https://azure.microsoft.com/en-us/pricing/details/container-registry/)
- [Azure Monitor pricing](https://azure.microsoft.com/en-us/pricing/details/monitor/)

Microsoft notes that actual prices vary by agreement, currency, and region.

## Security follow-up

The first deployment uses the Azure Container Registry admin account as a registry
pull secret for simplicity. For production, disable the ACR admin account after
moving the Container App to a managed identity with the `AcrPull` role.
