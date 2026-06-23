# cfai

IBM Code Engine deployment-ready Product Catalog API.

## Required structure

```text
cfai/
+-- Dockerfile
+-- app.py
+-- config/
¦   +-- requirements.txt
+-- data/
¦   +-- product_match_dictionary.json
+-- src/
    +-- api/
    +-- core/
    +-- utils/
```

## Deploy to Code Engine

Use [`CODE_ENGINE_DEPLOYMENT.md`](CODE_ENGINE_DEPLOYMENT.md) for deployment steps.

## Get endpoint URL after deployment

PowerShell:

```powershell
(ibmcloud ce application get --name product-catalog-api --output json | ConvertFrom-Json).status.url
```
