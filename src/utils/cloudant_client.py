"""
IBM Cloudant / local CouchDB client utilities.

Provides a lazy singleton Cloudant client and a helper to fetch the
product-match dictionary document from the configured database.

Authentication
--------------
Local CouchDB uses HTTP Basic Auth (CLOUDANT_USERNAME + CLOUDANT_PASSWORD).
IBM Cloudant hosted service uses IAM (CLOUDANT_APIKEY).
The module picks the right authenticator automatically:
  - CLOUDANT_APIKEY set   → IAMAuthenticator  (IBM Cloud hosted)
  - CLOUDANT_APIKEY unset → BasicAuthenticator (local CouchDB / ICP)
"""

import os
from typing import Any, Dict, Optional

from ibmcloudant.cloudant_v1 import CloudantV1
from ibm_cloud_sdk_core.authenticators import BasicAuthenticator, IAMAuthenticator
from ibm_cloud_sdk_core.api_exception import ApiException


_client: Optional[CloudantV1] = None


def get_cloudant_client() -> CloudantV1:
    """
    Return a cached Cloudant client, creating it on first call.

    Environment variables
    ---------------------
    CLOUDANT_URL       CouchDB / Cloudant service URL  (required)

    For local CouchDB (Basic Auth):
        CLOUDANT_USERNAME  CouchDB admin username
        CLOUDANT_PASSWORD  CouchDB admin password

    For IBM Cloud hosted Cloudant (IAM):
        CLOUDANT_APIKEY    IAM API key

    Raises
    ------
    RuntimeError if the required environment variables are not set.
    """
    global _client
    if _client is not None:
        return _client

    url = os.getenv("CLOUDANT_URL", "").strip()
    if not url:
        raise RuntimeError("CLOUDANT_URL environment variable must be set.")

    apikey = os.getenv("CLOUDANT_APIKEY", "").strip()

    if apikey:
        # IBM Cloud hosted Cloudant — IAM authentication
        authenticator = IAMAuthenticator(apikey)
    else:
        # Local CouchDB — Basic authentication
        username = os.getenv("CLOUDANT_USERNAME", "").strip()
        password = os.getenv("CLOUDANT_PASSWORD", "").strip()
        if not username or not password:
            raise RuntimeError(
                "For local CouchDB set CLOUDANT_USERNAME and CLOUDANT_PASSWORD "
                "(or set CLOUDANT_APIKEY for IBM Cloud hosted Cloudant)."
            )
        authenticator = BasicAuthenticator(username, password)

    _client = CloudantV1(authenticator=authenticator)
    _client.set_service_url(url)
    return _client


def load_match_dictionary_from_cloudant() -> Dict[str, Any]:
    """
    Fetch the product-match dictionary document from CouchDB / Cloudant.

    Environment variables
    ----------------------
    CLOUDANT_DB      Database name  (default: ``ibmproductdtool``)
    CLOUDANT_DOC_ID  Document _id   (default: ``ICR_Chat_Product_Match_Dictionary``)

    Returns
    -------
    dict
        The ``match_dictionary`` sub-document, i.e.::

            {
                "exact_match": {...},
                "fuzzy_match": {...}
            }

    Raises
    ------
    RuntimeError  if the document or match_dictionary key is missing.
    ibmcloudant errors  propagated as-is for the caller to handle.
    """
    db_name = os.getenv("CLOUDANT_DB", "ibmproductdtool")
    doc_id = os.getenv("CLOUDANT_DOC_ID", "ICR_Chat_Product_Match_Dictionary")

    client = get_cloudant_client()
    try:
        response = client.get_document(db=db_name, doc_id=doc_id).get_result()
    except ApiException as exc:
        if exc.status_code == 404:
            raise RuntimeError(
                f"CouchDB 404: database '{db_name}' or document '{doc_id}' does not exist. "
                f"Check that CLOUDANT_DB='{db_name}' matches the actual database name in your "
                "CouchDB/Cloudant instance, and that the document has been created."
            ) from exc
        raise

    match_dictionary = response.get("match_dictionary")
    if match_dictionary is None:
        raise RuntimeError(
            f"CouchDB document '{doc_id}' in database '{db_name}' "
            "does not contain a 'match_dictionary' key."
        )

    return match_dictionary


# Made with Bob
