# CfaiProductCatalogTool

High-performance FastAPI service for IBM product identification using a multi-stage search pipeline (Aho-Corasick, BM25, RapidFuzz, N-gram).

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

The `data/` folder is **not** in source control. Before building the image, place the product dictionary file locally:

```
data/
└── product_match_dictionary.json
```

The `Dockerfile` copies this directory into the image:

```dockerfile
COPY data/ ./data/
```

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
| `/docs` | GET | Swagger UI |
| `/redoc` | GET | ReDoc UI |
| `/search` | POST | Product search |
| `/products` | GET | Product listing |

---

### Step 9 — View live logs

```bash
# Stream logs
ibmcloud ce application logs --name cfai-product-catalog --follow

# Dump recent logs
ibmcloud ce application logs --name cfai-product-catalog
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_ENHANCED_MATCHER` | `true` | Enable full search pipeline (Aho-Corasick + BM25 + N-gram) |
| `PORT` | `8080` | Port the server listens on |
| `PYTHONUNBUFFERED` | `1` | Disable output buffering |
| `PYTHONDONTWRITEBYTECODE` | `1` | Skip `.pyc` generation |

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
├── app.py                          # ASGI entry point
├── Dockerfile                      # IBM Code Engine container definition
├── config/
│   ├── requirements.txt            # All dependencies (dev + prod)
│   └── requirements-prod.txt       # Production-only dependencies
├── src/
│   ├── api/
│   │   ├── app.py                  # FastAPI application factory & startup
│   │   └── routes/                 # health / search / products routers
│   ├── core/
│   │   └── matcher.py              # ProductMatcher (multi-stage search pipeline)
│   └── utils/                      # Shared utilities
└── data/                           # ⚠ NOT in source control — add locally before build
    └── product_match_dictionary.json

