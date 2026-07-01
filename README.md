# cfaiTool — Product Catalog API

High-performance FastAPI service for IBM product identification using a multi-stage search pipeline (Aho-Corasick, BM25, RapidFuzz, N-gram).

---

## Prerequisites

| Tool | Version |
|------|---------|
| IBM Cloud CLI | latest |
| Code Engine plug-in | `ibmcloud plugin install code-engine` |
| Container Registry plug-in | `ibmcloud plugin install container-registry` |
| Docker (local build) | 20+ |
| Python | 3.11+ |

---

## Deployment on IBM Code Engine

### 1. Login to IBM Cloud

```bash
ibmcloud login --sso
# or with API key
ibmcloud login --apikey <YOUR_IBM_CLOUD_API_KEY> -r <REGION> -g <RESOURCE_GROUP>
```

### 2. Target the Code Engine project

```bash
# List existing projects
ibmcloud ce project list

# Select an existing project
ibmcloud ce project select --name <YOUR_PROJECT_NAME>

# OR create a new project
ibmcloud ce project create --name cfaiTool
ibmcloud ce project select --name cfaiTool
```

### 3. Configure IBM Container Registry (ICR)

```bash
# Target the registry region (e.g. us-south)
ibmcloud cr region-set us-south

# Log Docker into ICR
ibmcloud cr login

# Create a namespace (one-time)
ibmcloud cr namespace-add <YOUR_NAMESPACE>
```

### 4. Build and push the Docker image

```bash
# Set your image tag
export IMAGE=us.icr.io/<YOUR_NAMESPACE>/cfaitool:latest

# Build
docker build -t $IMAGE .

# Push to ICR
docker push $IMAGE
```

> **Note:** The `data/` folder is **not** committed to source control.  
> Before building, ensure `data/product_match_dictionary.json` is present locally — it will be baked into the image at build time via the `COPY data/ ./data/` step in the Dockerfile.

### 5. Deploy the application to Code Engine

**First deployment:**

```bash
ibmcloud ce application create \
  --name cfaitool \
  --image $IMAGE \
  --registry-secret icr-secret \
  --port 8080 \
  --cpu 1 \
  --memory 2G \
  --min-scale 1 \
  --max-scale 5 \
  --env USE_ENHANCED_MATCHER=true
```

**Subsequent updates (re-deploy after image push):**

```bash
ibmcloud ce application update \
  --name cfaitool \
  --image $IMAGE
```

### 6. Create a registry pull secret (if not already present)

```bash
ibmcloud ce secret create-registry \
  --name icr-secret \
  --server us.icr.io \
  --username iamapikey \
  --password <YOUR_IBM_CLOUD_API_KEY>
```

### 7. Verify deployment

```bash
# Check application status
ibmcloud ce application get --name cfaitool

# Get the public URL
ibmcloud ce application get --name cfaitool --output url
```

The application exposes:

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /docs` | Swagger UI |
| `GET /redoc` | ReDoc UI |
| `POST /search` | Product search |
| `GET /products` | Product listing |

### 8. View logs

```bash
# Streaming logs
ibmcloud ce application logs --name cfaitool --follow

# Recent logs only
ibmcloud ce application logs --name cfaitool
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_ENHANCED_MATCHER` | `true` | Enable Aho-Corasick + BM25 + N-gram pipeline |
| `PORT` | `8080` | Port the server listens on |
| `PYTHONUNBUFFERED` | `1` | Disable output buffering |

---

## Local Development

```bash
# Create and activate virtual environment
python -m venv env
env\Scripts\activate        # Windows
source env/bin/activate     # Linux / macOS

# Install dependencies
pip install -r config/requirements-prod.txt

# Run locally
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

---

## Important — Files NOT committed to source control

The following are excluded via `.gitignore` and must **never** be pushed:

- `cfaiRS256.key` — private RS256 JWT signing key
- `cfaiRS256.key.pub` — public RS256 key
- `data/` — product dictionary (bake into image at build time)
- `env/` — local Python virtual environment

---

## Project Structure

```
cfaiTool/
├── app.py                        # ASGI entry point
├── Dockerfile                    # IBM Code Engine container definition
├── config/
│   ├── requirements.txt          # All dependencies
│   └── requirements-prod.txt     # Production-only dependencies
├── src/
│   ├── api/
│   │   ├── app.py                # FastAPI application factory
│   │   └── routes/               # health / search / products routers
│   ├── core/
│   │   └── matcher.py            # ProductMatcher (search pipeline)
│   └── utils/                    # Shared utilities
└── data/                         # ⚠ NOT in source control — add locally
    └── product_match_dictionary.json
```
