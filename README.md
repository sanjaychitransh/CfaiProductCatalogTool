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
