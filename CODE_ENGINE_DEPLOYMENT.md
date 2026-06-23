# IBM Code Engine Deployment Guide

This guide provides step-by-step instructions for deploying the Product Catalog API to IBM Code Engine.

## Prerequisites

- IBM Cloud account with Code Engine access
- IBM Cloud CLI installed
- Docker installed (for local testing)
- Code Engine plugin: `ibmcloud plugin install code-engine`

## Quick Deployment

### Option 1: Deploy from Source (Recommended)

```bash
# 1. Login to IBM Cloud
ibmcloud login --sso

# 2. Target your resource group
ibmcloud target -g <your-resource-group>

# 3. Create a Code Engine project (if not exists)
ibmcloud ce project create --name product-catalog-api

# 4. Select the project
ibmcloud ce project select --name product-catalog-api

# 5. Deploy the application from source
ibmcloud ce application create \
  --name product-catalog-api \
  --build-source . \
  --strategy dockerfile \
  --port 8080 \
  --min-scale 1 \
  --max-scale 5 \
  --cpu 0.5 \
  --memory 1G \
  --env USE_ENHANCED_MATCHER=true
```

### Option 2: Deploy from Container Registry

```bash
# 1. Build and push to IBM Container Registry
ibmcloud cr login
ibmcloud cr namespace-add product-catalog

# Build the image
docker build -t us.icr.io/product-catalog/product-catalog-api:latest .

# Push to registry
docker push us.icr.io/product-catalog/product-catalog-api:latest

# 2. Deploy to Code Engine
ibmcloud ce application create \
  --name product-catalog-api \
  --image us.icr.io/product-catalog/product-catalog-api:latest \
  --registry-secret icr-secret \
  --port 8080 \
  --min-scale 1 \
  --max-scale 5 \
  --cpu 0.5 \
  --memory 1G \
  --env USE_ENHANCED_MATCHER=true
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Application port (Code Engine default) |
| `USE_ENHANCED_MATCHER` | `true` | Enable enhanced search features |
| `PYTHONUNBUFFERED` | `1` | Disable Python output buffering |

### Resource Allocation

**Recommended Settings:**
- **CPU**: 0.5 vCPU (can scale to 1 vCPU for high load)
- **Memory**: 1 GB (minimum for product dictionary)
- **Min Scale**: 1 (always-on for fast response)
- **Max Scale**: 5 (adjust based on traffic)
- **Concurrency**: 100 (default, adjust if needed)

### Scaling Configuration

```bash
# Update scaling settings
ibmcloud ce application update product-catalog-api \
  --min-scale 1 \
  --max-scale 10 \
  --concurrency 100 \
  --cpu 1 \
  --memory 2G
```

## Testing the Deployment

```bash
# Get the application URL
ibmcloud ce application get --name product-catalog-api

# Get only the endpoint URL in JSON output
ibmcloud ce application get --name product-catalog-api --output json

# Linux/macOS: extract only the URL
ibmcloud ce application get --name product-catalog-api --output json | grep -o '"url":"[^"]*' | cut -d'"' -f4

# PowerShell: extract only the URL
(ibmcloud ce application get --name product-catalog-api --output json | ConvertFrom-Json).status.url

# Test health endpoint
curl https://<your-app-url>/health

# Test search endpoint
curl -X POST https://<your-app-url>/search \
  -H "Content-Type: application/json" \
  -d '{"query": "watson assistant"}'
```

## Required File Structure for Code Engine

Use this project structure when deploying from source to IBM Code Engine:

```text
CAFI-product/
├── Dockerfile
├── app.py
├── config/
│   └── requirements.txt
├── data/
│   └── product_match_dictionary.json
└── src/
    ├── api/
    ├── core/
    └── utils/
```

### Required files

- [`Dockerfile`](Dockerfile) builds the container image for Code Engine.
- [`app.py`](app.py) exposes the ASGI application entry point used by [`uvicorn.run()`](app.py:16).
- [`config/requirements.txt`](config/requirements.txt) contains Python dependencies installed during image build.
- `data/product_match_dictionary.json` is required at runtime because the app loads it in [`startup_event()`](src/api/app.py:52) via [`open()`](src/api/app.py:57).
- [`src/api/app.py`](src/api/app.py) defines the FastAPI app and routes.

### Endpoint URL after deployment

After deployment, the public endpoint URL is not stored in a source file. IBM Code Engine generates it for the deployed application. Retrieve it with:

```bash
ibmcloud ce application get --name product-catalog-api --output json
```

Look for:

```json
{
  "status": {
    "url": "https://product-catalog-api.<generated-domain>"
  }
}
```

## Monitoring

### View Logs

```bash
# Stream application logs
ibmcloud ce application logs --name product-catalog-api --follow

# View recent logs
ibmcloud ce application logs --name product-catalog-api --tail 100
```

### Check Application Status

```bash
# Get application details
ibmcloud ce application get --name product-catalog-api

# List all applications
ibmcloud ce application list
```

### Metrics and Monitoring

Access metrics through IBM Cloud Console:
1. Navigate to Code Engine project
2. Select your application
3. View metrics: requests, response time, CPU, memory

## Updating the Application

### Update from Source

```bash
ibmcloud ce application update product-catalog-api \
  --build-source . \
  --strategy dockerfile
```

### Update from New Image

```bash
# Build and push new image
docker build -t us.icr.io/product-catalog/product-catalog-api:v2 .
docker push us.icr.io/product-catalog/product-catalog-api:v2

# Update application
ibmcloud ce application update product-catalog-api \
  --image us.icr.io/product-catalog/product-catalog-api:v2
```

## Local Testing with Docker

```bash
# Build the image
docker build -t product-catalog-api:local .

# Run locally
docker run -p 8080:8080 \
  -e USE_ENHANCED_MATCHER=true \
  product-catalog-api:local

# Test locally
curl http://localhost:8080/health
```

## Troubleshooting

### Application Won't Start

```bash
# Check logs for errors
ibmcloud ce application logs --name product-catalog-api --tail 200

# Common issues:
# - Missing data/product_match_dictionary.json file
# - Insufficient memory (increase to 2G)
# - Port mismatch (ensure PORT=8080)
```

### High Memory Usage

```bash
# Increase memory allocation
ibmcloud ce application update product-catalog-api --memory 2G

# Or reduce workers
# Modify Dockerfile CMD to: --workers 1
```

### Slow Response Times

```bash
# Increase CPU allocation
ibmcloud ce application update product-catalog-api --cpu 1

# Increase min-scale to keep instances warm
ibmcloud ce application update product-catalog-api --min-scale 2
```

### Build Failures

```bash
# Check build logs
ibmcloud ce buildrun logs --name <buildrun-name>

# Common issues:
# - Missing dependencies in requirements.txt
# - Dockerfile syntax errors
# - Large image size (optimize with .dockerignore)
```

## Cost Optimization

### Development Environment
```bash
# Minimal resources for testing
ibmcloud ce application update product-catalog-api \
  --min-scale 0 \
  --max-scale 2 \
  --cpu 0.25 \
  --memory 512M
```

### Production Environment
```bash
# Optimized for performance and availability
ibmcloud ce application update product-catalog-api \
  --min-scale 2 \
  --max-scale 10 \
  --cpu 1 \
  --memory 2G \
  --concurrency 100
```

## Security Best Practices

1. **Use Private Endpoints**: Enable private endpoints for internal services
2. **API Authentication**: Add authentication middleware (not included by default)
3. **HTTPS Only**: Code Engine provides automatic HTTPS
4. **Secrets Management**: Use Code Engine secrets for sensitive data
5. **Network Policies**: Configure network policies if needed

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Deploy to Code Engine

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Install IBM Cloud CLI
        run: |
          curl -fsSL https://clis.cloud.ibm.com/install/linux | sh
          ibmcloud plugin install code-engine
      
      - name: Login to IBM Cloud
        run: |
          ibmcloud login --apikey ${{ secrets.IBM_CLOUD_API_KEY }} -r us-south
          ibmcloud target -g ${{ secrets.RESOURCE_GROUP }}
      
      - name: Deploy to Code Engine
        run: |
          ibmcloud ce project select --name product-catalog-api
          ibmcloud ce application update product-catalog-api \
            --build-source . \
            --strategy dockerfile
```

## Additional Resources

- [IBM Code Engine Documentation](https://cloud.ibm.com/docs/codeengine)
- [Code Engine CLI Reference](https://cloud.ibm.com/docs/codeengine?topic=codeengine-cli)
- [FastAPI Deployment Best Practices](https://fastapi.tiangolo.com/deployment/)

## Support

For issues or questions:
1. Check application logs: `ibmcloud ce application logs`
2. Review Code Engine documentation
3. Contact IBM Cloud support

---

**Last Updated**: 2026-06-19
**Version**: 1.0.0