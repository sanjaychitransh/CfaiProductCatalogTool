# CfaiProductCatalogTool

High-performance FastAPI service for IBM product identification using a multi-stage search pipeline (Aho-Corasick, BM25, RapidFuzz, N-gram).

---

## What's New in v4 — TLS Routing

When a user query matches a **TLS-owned product** (identified by its SLC code), the primary search endpoint (`GET /products/search`) now returns a **redirect response** instead of product names:

```json
{
  "tls_product": true,
  "message": "This is a TLS product. Redirected to TLS agent.",
  "slc_code": "SCPA0",
  "product_name": "AIX",
  "assistant": "AIX (Support)"
}
```

The caller should hand the conversation off to the named TLS agent.

**Fallback:** The original behaviour (no TLS check, raw product results) is preserved at `GET /v0/products/search` for regression testing, debugging, or rollback.

---

## Prerequisites

| Tool | Minimum Version | Install |
|------|----------------|---------|
| IBM Cloud CLI | latest | https://cloud.ibm.com/docs/cli |
| Code Engine plug-in | latest | `ibmcloud plugin install code-engine` |
| Container Registry plug-in | latest | `ibmcloud plugin install container-registry` |
| Docker | 20+ | https://docs.docker.com/get-docker/ |

---

## Deployment on IBM Code Engine

### Step 1 — Log in to IBM Cloud

```bash
# Interactive SSO login
ibmcloud login --sso

# OR API-key login (non-interactive / CI)
ibmcloud login --apikey <YOUR_IBM_CLOUD_API_KEY> -r <REGION> -g <RESOURCE_GROUP>
# Example: -r us-south -g Default
```

---

### Step 2 — Target a Code Engine project

```bash
# List existing projects
ibmcloud ce project list

# Select an existing project
ibmcloud ce project select --name <YOUR_PROJECT_NAME>

# OR create a new one
ibmcloud ce project create --name CfaiProductCatalogTool
ibmcloud ce project select --name CfaiProductCatalogTool
```

---

### Step 3 — Log in to IBM Container Registry (ICR)

```bash
# Set the registry region (e.g. us-south)
ibmcloud cr region-set us-south

# Log Docker into ICR
ibmcloud cr login

# Create a namespace (one-time)
ibmcloud cr namespace-add <YOUR_NAMESPACE>
```

---

### Step 4 — Prepare the data directory

The `data/` folder is **not** in source control. Before building the image, place the required files locally:

```
data/
├── product_match_dictionary.json
└── tls_assistant_slc_code_mappings.json   ← required for TLS routing
```

The `Dockerfile` copies this directory into the image:

```dockerfile
COPY data/ ./data/
```

> **Note:** `tls_assistant_slc_code_mappings.json` must contain only entries with `"owner": "TLS"`.
> If the file is missing, TLS routing is disabled and the service starts normally with a warning.

---

### Step 5 — Build and push the Docker image

```bash
# Define your image tag
export IMAGE=us.icr.io/<YOUR_NAMESPACE>/cfai-product-catalog:latest

# Build the image
docker build -t $IMAGE .

# Push to IBM Container Registry
docker push $IMAGE
```

---

### Step 6 — Create a registry pull secret

This is a one-time step so Code Engine can pull from ICR.

```bash
ibmcloud ce secret create-registry \
  --name icr-secret \
  --server us.icr.io \
  --username iamapikey \
  --password <YOUR_IBM_CLOUD_API_KEY>
```

---

### Step 7 — Deploy the application

**First deployment:**

```bash
ibmcloud ce application create \
  --name cfai-product-catalog \
  --image $IMAGE \
  --registry-secret icr-secret \
  --port 8080 \
  --cpu 1 \
  --memory 2G \
  --min-scale 1 \
  --max-scale 5 \
  --env USE_ENHANCED_MATCHER=true
```

**Re-deploy after updating the image:**

```bash
ibmcloud ce application update \
  --name cfai-product-catalog \
  --image $IMAGE
```

---

### Step 8 — Verify the deployment

```bash
# Check application status
ibmcloud ce application get --name cfai-product-catalog

# Get the public URL
ibmcloud ce application get --name cfai-product-catalog --output url
```

Open the returned URL in a browser — the following endpoints are available:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/stats` | GET | Matcher statistics |
| `/docs` | GET | Swagger UI |
| `/redoc` | GET | ReDoc UI |
| `/products/search` | GET | **Primary** product search (with TLS intercept) |
| `/v0/products/search` | GET | **Secondary** product search (no TLS check — fallback) |
| `/products` | GET | Product listing |
| `/products/{SLC_CODE}` | GET | Get product by SLC code |

---

### Step 9 — View live logs

```bash
# Stream logs
ibmcloud ce application logs --name cfai-product-catalog --follow

# Dump recent logs
ibmcloud ce application logs --name cfai-product-catalog
```

---

## TLS Routing

### How it works

1. A query arrives at `GET /products/search`.
2. The matcher runs its normal multi-stage search pipeline.
3. **Before** building the response, the top result's `product_code` (SLC code) is looked up in `tls_assistant_slc_code_mappings.json`.
4. If the SLC code belongs to a TLS-owned product:
   - The endpoint returns a `TLSRedirectResponse` — **no product names** are included.
   - The `assistant` field names the TLS agent the caller should route to.
5. If the SLC code is **not** a TLS product, the normal `SearchResponse` is returned.

### TLS response shape

```json
{
  "tls_product": true,
  "message": "This is a TLS product. Redirected to TLS agent.",
  "slc_code": "SCPA0",
  "product_name": "AIX",
  "assistant": "AIX (Support)"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `tls_product` | boolean | Always `true` when this response is returned |
| `message` | string | Human-readable redirect notice |
| `slc_code` | string | Matched SLC code |
| `product_name` | string | TLS product name |
| `assistant` | string | Name of the TLS agent to redirect to |

### Fallback / bypass

Use `GET /v0/products/search` to get the raw product results without TLS interception.
This endpoint is identical to the pre-v4 primary endpoint — useful for:

- Comparing results with / without TLS routing
- Debugging which products are being intercepted
- Rolling back temporarily without a re-deploy

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_ENHANCED_MATCHER` | `true` | Enable full search pipeline (Aho-Corasick + BM25 + N-gram) |
| `USE_SBERT_RERANKER` | `false` | Enable Sentence-BERT + Cross-Encoder semantic re-ranking stage |
| `SBERT_MODEL` | `all-MiniLM-L6-v2` | Bi-encoder model name (HuggingFace Hub or local path) |
| `CROSS_ENCODER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder model name |
| `PORT` | `8080` | Port the server listens on |
| `PYTHONUNBUFFERED` | `1` | Disable output buffering |
| `PYTHONDONTWRITEBYTECODE` | `1` | Skip `.pyc` generation |

### Semantic Re-Ranking (v6)

When `USE_SBERT_RERANKER=true` the pipeline gains a semantic stage **after** the
existing lexical matching:

1. All matched-product aliases are encoded by the **Sentence-BERT bi-encoder**
   (`all-MiniLM-L6-v2` by default, ~90 MB). Cosine similarity selects the
   top-20 semantically closest aliases.
2. Each `(query, alias)` pair in that short-list is scored by the
   **Cross-Encoder** (`cross-encoder/ms-marco-MiniLM-L-6-v2`), which reads both
   strings jointly for high-accuracy relevance scoring.
3. A **blended score** (70 % confidence + 30 % cross-encoder, or 85/15 for
   exact-match results) re-orders the final result list.

The existing Aho-Corasick → BM25 → RapidFuzz → Confidence pipeline is
**completely unchanged** — the SBERT stage is a pure post-processor.

---

## Deployment on Azure Container Apps

Azure Container Apps runs this FastAPI service from the supplied `Dockerfile`.
It exposes the container's port `8080` through a managed HTTPS endpoint.

### Prerequisites

- An Azure subscription where your account has the **Contributor** role.
- Azure CLI installed (`az --version`).
- The required data files present before building the image:

  ```text
  data/product_match_dictionary.json
  data/tls_assistant_slc_code_mappings.json
  ```

If your organization requires MFA, sign in with device-code login. Complete the
prompt in the browser before continuing:

```powershell
az login --use-device-code --tenant <TENANT_ID>
az account list --output table
az account set --subscription "<SUBSCRIPTION_ID_OR_NAME>"
```

`az account list` must show at least one subscription. If it does not, the
Azure subscription owner must grant the signed-in account access before a
deployment can be created.

### Create Azure resources and build the image

Run these commands in PowerShell from the project root. Replace the region if
needed. The registry name must be globally unique, use lowercase letters and
numbers only, and be 5--50 characters long.

```powershell
$RESOURCE_GROUP = "cfai-product-catalog-rg"
$LOCATION = "centralindia"
$ENVIRONMENT = "cfai-product-catalog-env"
$REGISTRY = "<UNIQUE_ACR_NAME>"
$APP = "cfai-product-catalog"
$TAG = "v1"

az group create --name $RESOURCE_GROUP --location $LOCATION
az extension add --name containerapp --upgrade

az containerapp env create `
  --name $ENVIRONMENT `
  --resource-group $RESOURCE_GROUP `
  --location $LOCATION

az acr create `
  --name $REGISTRY `
  --resource-group $RESOURCE_GROUP `
  --sku Basic `
  --admin-enabled true

az acr build --registry $REGISTRY --image "$APP`:$TAG" .
```

The image is built in Azure Container Registry, so Docker does not need to be
installed locally. ACR's admin credentials are used below as the simplest
initial registry connection; use a managed identity and `AcrPull` role for a
production deployment.

### Deploy and verify

```powershell
$LOGIN_SERVER = az acr show --name $REGISTRY --resource-group $RESOURCE_GROUP --query loginServer --output tsv
$ACR_USERNAME = az acr credential show --name $REGISTRY --query username --output tsv
$ACR_PASSWORD = az acr credential show --name $REGISTRY --query "passwords[0].value" --output tsv

az containerapp create `
  --name $APP `
  --resource-group $RESOURCE_GROUP `
  --environment $ENVIRONMENT `
  --image "$LOGIN_SERVER/$APP`:$TAG" `
  --registry-server $LOGIN_SERVER `
  --registry-username $ACR_USERNAME `
  --registry-password $ACR_PASSWORD `
  --target-port 8080 `
  --ingress external `
  --min-replicas 1 `
  --max-replicas 5 `
  --cpu 1.0 `
  --memory 2.0Gi `
  --env-vars USE_ENHANCED_MATCHER=true PORT=8080

$FQDN = az containerapp show --name $APP --resource-group $RESOURCE_GROUP --query properties.configuration.ingress.fqdn --output tsv
Invoke-WebRequest "https://$FQDN/health"
```

The API documentation is available at `https://<FQDN>/docs`. The health endpoint
is public; the other API endpoints require the Bearer token configured by this
application.

### Deploy an update

Build a new immutable image tag, then point the Container App at it:

```powershell
$TAG = "v2"
az acr build --registry $REGISTRY --image "$APP`:$TAG" .
az containerapp update `
  --name $APP `
  --resource-group $RESOURCE_GROUP `
  --image "$LOGIN_SERVER/$APP`:$TAG"
```

To inspect a failed deployment or application startup issue:

```powershell
az containerapp logs show --name $APP --resource-group $RESOURCE_GROUP --follow
```

### Deployment report — 19 September 2026

The complete deployment outcome, issues, Azure services, and cost analysis are
available in [AZURE_DEPLOYMENT_REPORT.md](AZURE_DEPLOYMENT_REPORT.md).

This deployment was completed in the `centralindia` region using subscription
`Azure subscription 1`. The public application URL is:

```text
https://cfai-product-catalog.politedesert-8cf38796.centralindia.azurecontainerapps.io/
```

| Check | Result | Evidence / outcome |
|---|---|---|
| Azure authentication | Succeeded | The selected subscription is enabled. |
| Resource group | Succeeded | `cfai-product-catalog-rg` was created. |
| Container Apps environment | Succeeded | `cfai-product-catalog-env` provisioned successfully. |
| Container registry | Succeeded | Basic registry `cfaipcat265c150a` was created. |
| Image | Succeeded | Registry contains `cfai-product-catalog:v1`. |
| Container App revision | Succeeded | Active revision `cfai-product-catalog--blpg2gj` is provisioned with one replica. |
| External health check | Succeeded | `GET /health` returned HTTP 200. |
| Swagger UI | Available | Open `https://cfai-product-catalog.politedesert-8cf38796.centralindia.azurecontainerapps.io/docs`. |

#### Issues encountered and resolution

| Issue | Resolution |
|---|---|
| `az` was not on the initial terminal `PATH`. | Azure CLI was invoked from its Windows installation path; open a new terminal after installation to use `az` normally. |
| The first signed-in account had no visible subscription and MFA was required. | Device-code/MFA sign-in was completed and the enabled subscription was selected. |
| `Microsoft.App`, `Microsoft.ContainerRegistry`, and `Microsoft.OperationalInsights` were not registered. | The required resource providers were registered before provisioning continued. |
| The managed environment initially reported `Waiting`. | Azure completed its asynchronous environment and Log Analytics workspace provisioning; final state was `Succeeded`. |
| ACR source packaging included the local `env/` folder (about 1.2 GB), causing unnecessary queued build attempts. | A minimal temporary build context was submitted. `.dockerignore` was added to prevent local environments from entering Docker build contexts. Obsolete queued quick-build runs were cancelled. |
| Local Docker was unavailable. | The image was built with `az acr build` in Azure Container Registry, so no local Docker daemon was required. |

#### Azure services used

| Service | Deployed resource | Purpose | Cost behaviour |
|---|---|---|---|
| Azure Resource Manager | `cfai-product-catalog-rg` | Logical resource group for all deployment resources. | No standalone charge. |
| Azure Container Apps (Consumption) | `cfai-product-catalog` | Runs the FastAPI container with public HTTPS ingress on port 8080. Configured for 1 vCPU, 2 GiB memory, minimum 1 and maximum 5 replicas. | Compute, memory, and requests are billed by use; the minimum replica can incur idle charges. |
| Azure Container Apps managed environment | `cfai-product-catalog-env` | Shared hosting environment, networking, revisions, and log connection. | No separate dedicated-plan charge was configured; associated log ingestion may cost money. |
| Azure Container Registry, Basic SKU | `cfaipcat265c150a` | Stores `cfai-product-catalog:v1` and performed cloud image builds. | Basic tier daily registry charge, included storage allowance, potential extra storage, and per-second ACR Tasks build charges. |
| Azure Monitor / Log Analytics | `workspace-cfaiproductcatalogrgN31b` | Receives Container Apps platform and application logs. | Log ingestion, extended retention, alerts, and data export can incur charges. |

No database, Key Vault, Storage Account, Application Gateway, CDN, virtual network,
or static public IP was created by this deployment.

#### Cost baseline and controls

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

See the official [Azure Container Apps pricing](https://azure.microsoft.com/en-us/pricing/details/container-apps/), [Azure Container Registry pricing](https://azure.microsoft.com/en-us/pricing/details/container-registry/), and [Azure Monitor pricing](https://azure.microsoft.com/en-us/pricing/details/monitor/) pages for the current rate card. Microsoft notes that actual prices vary by agreement, currency, and region.

> Security follow-up: the first deployment uses the ACR admin account as a registry
> pull secret for simplicity. For production, disable the ACR admin account after
> moving the Container App to a managed identity with the `AcrPull` role.

## Local Development

```bash
# Create and activate virtual environment
python -m venv env
env\Scripts\activate        # Windows
source env/bin/activate     # Linux / macOS

# Install dependencies
pip install -r config/requirements-prod.txt

# Run locally (requires data/ to be present)
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

---

## Project Structure

```
CfaiProductCatalogTool/
├── app.py                                   # ASGI entry point
├── Dockerfile                               # IBM Code Engine container definition
├── product-catalog-api-openapi.yaml         # OpenAPI 3.0 spec (v4)
├── ibm-orchestrate-agent.yaml               # IBM Orchestrate agent definition
├── config/
│   ├── requirements.txt                     # All dependencies (dev + prod)
│   └── requirements-prod.txt               # Production-only dependencies
├── src/
│   ├── api/
│   │   ├── app.py                           # FastAPI application factory & startup
│   │   ├── models/
│   │   │   └── response.py                  # Pydantic response models (incl. TLSRedirectResponse)
│   │   └── routes/
│   │       ├── health.py                    # /health, /stats
│   │       ├── products.py                  # /products, /products/{SLC_CODE}
│   │       ├── search.py                    # /products/search  ← PRIMARY (TLS intercept)
│   │       └── search_v0.py                 # /v0/products/search ← SECONDARY (no TLS check)
│   ├── core/
│   │   ├── matcher.py                       # ProductMatcher (multi-stage search pipeline)
│   │   ├── matcher_enhanced.py              # Enhanced matcher helpers
│   │   ├── confidence_scorer.py             # Confidence scoring logic
│   │   └── tls_checker.py                   # TLS SLC-code lookup (new in v4)
│   └── utils/                              # Shared utilities
└── data/                                   # ⚠ NOT in source control — add locally before build
    ├── product_match_dictionary.json
    └── tls_assistant_slc_code_mappings.json
```
