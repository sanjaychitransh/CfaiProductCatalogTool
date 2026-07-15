"""
TLS Routing Test Suite
======================
Tests the /products/search endpoint against known TLS and non-TLS products.

For TLS products   → expects { "tls_product": true, "slc_code": <code> }
For non-TLS products → expects { "results": [...] } (NO tls_product field)

Usage
-----
# Against live IBM Cloud deployment (default)
python tests/test_tls_routing.py

# Against local server
python tests/test_tls_routing.py --base-url http://localhost:8080

# Verbose (show full API response for every case)
python tests/test_tls_routing.py --verbose

# Save results to JSON
python tests/test_tls_routing.py --output results.json
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
import urllib.request
import urllib.parse
import urllib.error

# ---------------------------------------------------------------------------
# Test data — sourced directly from the provided mapping table
# ---------------------------------------------------------------------------

# (product_name, slc_code, assistant)
TLS_PRODUCTS = [
    # AIX
    ("AIX",                                             "SCPA0",   "AIX (Support)"),
    ("AIX on PowerVS",                                  "SCPL3",   "AIX (Support)"),
    ("PowerVM / VIOS",                                  "SCPE7",   "AIX (Support)"),
    # DS8000
    ("DS8870",                                          "SIS0A99", "DS8000 (Support)"),
    ("DS8900F",                                         "SIS0AA1", "DS8000 (Support)"),
    ("DS8880",                                          "SIS0AA0", "DS8000 (Support)"),
    # HMC
    ("Hardware Management Console Application",         "SCPB6",   "HMC (Support)"),
    # IBM i
    ("IBM i",                                           "SCPF9",   "IBM i (Support)"),
    ("IBM i on PowerVS",                                "SCPMH",   "IBM i (Support)"),
    ("WebSphere Application Server for IBM i",          "SCPL5",   "IBM i (Support)"),
    # MVS Microsoft
    ("Windows",                                         "SFSN9",   "MVS Microsoft (Support)"),
    ("Azure",                                           "SFSN8",   "MVS Microsoft (Support)"),
    ("M365 Platform",                                   "SMM3A04", "MVS Microsoft (Support)"),
    # MVS Cisco
    ("Nexus Series",                                    "SMC0A45", "MVS CISCO Network (Support)"),
    ("Firepower",                                       "SMC0A25", "MVS CISCO Network (Support)"),
    ("Hyperflex HX Series",                             "SMC0A8C", "MVS CISCO Network (Support)"),
    ("Router Series",                                   "SMC0A01", "MVS CISCO Network (Support)"),
    # MVS Linux
    ("Red Hat Enterprise Linux Server",                 "SCPK1",   "MVS Linux (Support)"),
    ("Red Hat OpenShift Container Platform",            "SFSO1",   "MVS Linux (Support)"),
    ("SUSE Linux Enterprise Server",                    "SCPG6",   "MVS Linux (Support)"),
    ("SAP HANA",                                        "SCPG3",   "MVS Linux (Support)"),
    ("Ubuntu Linux Server",                             "SCPH1",   "MVS Linux (Support)"),
    # Power Servers
    ("Power System S1024 Server",                       "SIP0B68", "Power Servers (Support)"),
    ("Power System E1080 Server",                       "SIP0B65", "Power Servers (Support)"),
    ("Power System AC922 Server",                       "SIP0A53", "Power Servers (Support)"),
    ("Power System LC922 Server",                       "SIP0B4F", "Power Servers (Support)"),
    # PowerHA
    ("VM Recovery Manager",                             "SCPB5",   "PowerHA (Support)"),
    ("PowerHA SystemMirror",                            "SCPE3",   "PowerHA (Support)"),
    # SAN
    ("SAN Volume Controller",                           "SIS0A35", "Storage FlashSystem (Support)"),
    ("SAN b-type Collection",                           "SIS0A05", "SAN (Support)"),
    ("SANnav",                                          "SIS0ACH", "SAN (Support)"),
    # Storage FlashSystem
    ("FlashSystem 5200",                                "SIS0AC8", "Storage FlashSystem (Support)"),
    ("FlashSystem 7300",                                "SIS0ACG", "Storage FlashSystem (Support)"),
    ("FlashSystem 9500",                                "SIS0ACF", "Storage FlashSystem (Support)"),
    ("Spectrum Virtualize for SAN Volume Controller",   "SIS0A77", "Storage FlashSystem (Support)"),
    ("Storwize V7000",                                  "SIS0AA8", "Storage FlashSystem (Support)"),
    # Storage Protect
    ("Storage Protect",                                 "SCSA6",   "Storage Protect (Support)"),
    ("Storage Protect for Virtual Environments",        "SCSC2",   "Storage Protect (Support)"),
    # Storage Scale
    ("Storage Scale",                                   "SCSB6",   "Storage Scale (Support)"),
    # Tape Storage
    ("TS4300 Tape Library",                             "SIS0A82", "Tape Storage (Support)"),
    ("TS7700",                                          "SIS0A49", "Tape Storage (Support)"),
    # Z Servers
    ("z16",                                             "SIZ0A2N", "Z Servers (Support)"),
    ("z15",                                             "SIZ0A20", "Z Servers (Support)"),
    # Z DevOps
    ("watsonx Code Assistant for Z",                    "SCZAC",   "Z DevOps (Support)"),
    ("Explorer for z/OS",                               "SCZV1",   "Z DevOps (Support)"),
    # z/OS
    ("z/OS",                                            "SCZQ9",   "z/OS (Support)"),
    ("DFSORT",                                          "SCZO6",   "z/OS (Support)"),
    # Storage Insights / Fusion
    ("Storage Insights",                                "SCSS4",   "Storage Insights (Support)"),
    ("Storage Fusion",                                  "SCSTX",   "Storage Fusion (Support)"),
]

# (product_name, slc_code, assistant)
NON_TLS_PRODUCTS = [
    ("API Connect",                                      "SAIQ2",  "API Connect (Support)"),
    ("Aspera",                                           "SAIT9",  "Aspera (Support)"),
    ("Business Automation Workflow",                     "SAIN8",  "Business Automation Workflow (Support)"),
    ("CICS Transaction Server",                          "SCZJ2",  "CICS (Support)"),
    ("Cloud Pak for AIOps",                              "SAJE7",  "CP4AIOps (Support)"),
    ("Cloud Pak for Data",                               "SAAD9",  "Cloud Pak for Data (Support)"),
    ("Cloud Pak for Integration",                        "SAJB2",  "Cloud Pak for Integration (Support)"),
    ("Cloudera Data Platform Private Cloud",             "SABE2",  "Cloudera Data Platform (Support)"),
    ("Db2 for z/OS",                                     "SAAI8",  "Db2 for zOS (Support)"),
    ("Db2 Linux, Unix and Windows",                      "SAAA0",  "Db2 Linux, Unix and Windows (Support)"),
    ("Engineering Requirements Management DOORS",        "SBIA7",  "Engineering Lifecycle Management (Support)"),
    ("Engineering Workflow Management",                  "SBIA1",  "Engineering Lifecycle Management (Support)"),
    ("Envizi ESG Suite",                                 "SBW03",  "Envizi (Support)"),
    ("FileNet Content Manager",                          "SAIL4",  "FileNet Content Manager (Support)"),
    ("Financial Transaction Manager",                    "SBFF3",  "Financial Transaction Manager (Support)"),
    ("Guardium Data Protection",                         "SBSE2",  "Guardium Data Protection (Support)"),
    ("IMS",                                              "SCZA1",  "IMS (Support)"),
    ("Informix Dynamic Server",                          "SAAG3",  "Informix Dynamic Server (Support)"),
    ("Instana Observability",                            "SAJG6",  "Instana (Support)"),
    ("Jazz for Service Management DASH / TIP",           "SAID8",  "Jazz.net"),
    ("MaaS360",                                          "SBSH2",  "MaaS360 (Support)"),
    ("Maximo Asset Management",                          "SBIF8",  "Maximo Application Suite (Support)"),
    ("Maximo Application Suite",                         "SBIK2",  "Maximo Application Suite (Support)"),
    ("Netcool/OMNIbus",                                  "SAIC0",  "Netcool Omnibus (Support)"),
    ("OpenPages",                                        "SBFD6",  "OpenPages (Support)"),
    ("QRadar SIEM",                                      "SBSC3",  "QRadar SIEM (Support)"),
    ("SPSS Statistics",                                  "SAAA9",  "SPSS (Support)"),
    ("Safer Payments",                                   "SBFE4",  "Safer Payments (Support)"),
    ("Sterling Order Management",                        "SBWI5",  "Sterling OMS (Support)"),
    ("Turbonomic On-Premises",                           "SAJD9",  "Turbonomic (Support)"),
    ("Verify Identity Access",                           "SBSD1",  "Verify (Support)"),
    ("WebSphere Application Server",                     "SAIM8",  "WebSphere Application Server (Support)"),
    ("Workload Scheduler",                               "SAIP6",  "Workload Scheduler (Support)"),
    ("Spectrum Control",                                 "SCSA5",  "Spectrum Control (Support)"),
    ("DevOps Deploy",                                    "SAIT2",  "DevOps Deploy (Support)"),
    ("DataPower",                                        "SAII7",  "DataPower (Support)"),
    ("Operational Decision Manager",                     "SAIP4",  "Operational Decision Manager (Support)"),
    ("NS1 Connect",                                      "SBFI5",  "NS1 Connect (Support)"),
    ("SevOne Network Performance Management",            "SAJG5",  "SevOne NPM (Support)"),
    ("InfoSphere Information Server: Data Integration",  "SAAD0",  "DataStage (Support)"),
]


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    product_name: str
    slc_code: str
    expected_type: str          # "tls" | "non_tls"
    expected_assistant: str
    query: str
    passed: bool
    actual_type: str            # "tls" | "non_tls" | "error" | "no_results"
    actual_slc: Optional[str]
    actual_assistant: Optional[str]
    top_score: Optional[float]
    execution_ms: float
    error_msg: str = ""
    raw_response: dict = field(default_factory=dict)


def call_api(base_url: str, query: str, timeout: int = 15) -> tuple[dict, float]:
    """Call the primary search endpoint and return (response_dict, elapsed_ms)."""
    params = urllib.parse.urlencode({"query": query, "limit": 10, "threshold": 0.70})
    url = f"{base_url.rstrip('/')}/products/search?{params}"
    t0 = time.time()
    try:
        req = urllib.request.urlopen(url, timeout=timeout)
        body = req.read().decode("utf-8")
        elapsed = (time.time() - t0) * 1000
        return json.loads(body), elapsed
    except urllib.error.HTTPError as e:
        elapsed = (time.time() - t0) * 1000
        body = e.read().decode("utf-8")
        return {"_http_error": e.code, "_body": body}, elapsed
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        return {"_exception": str(e)}, elapsed


def run_test(base_url: str, product_name: str, slc_code: str,
             expected_type: str, expected_assistant: str,
             verbose: bool = False) -> TestResult:

    query = product_name
    response, elapsed = call_api(base_url, query)

    result = TestResult(
        product_name=product_name,
        slc_code=slc_code,
        expected_type=expected_type,
        expected_assistant=expected_assistant,
        query=query,
        passed=False,
        actual_type="error",
        actual_slc=None,
        actual_assistant=None,
        top_score=None,
        execution_ms=elapsed,
        raw_response=response,
    )

    # Handle network / HTTP errors
    if "_exception" in response:
        result.error_msg = response["_exception"]
        return result
    if "_http_error" in response:
        result.error_msg = f"HTTP {response['_http_error']}: {response.get('_body','')[:120]}"
        return result

    # TLS response
    if response.get("tls_product") is True:
        result.actual_type = "tls"
        result.actual_slc = response.get("slc_code")
        result.actual_assistant = response.get("assistant")

        if expected_type == "tls":
            # Pass: got TLS redirect and it's the right SLC code
            result.passed = (result.actual_slc == slc_code)
            if not result.passed:
                result.error_msg = (
                    f"Wrong SLC: got {result.actual_slc} expected {slc_code}"
                )
        else:
            result.error_msg = (
                f"Expected normal result but got TLS redirect "
                f"(slc={result.actual_slc}, assistant={result.actual_assistant})"
            )

    # Normal response
    elif "results" in response:
        results = response["results"]
        result.actual_type = "non_tls"

        if results:
            result.actual_slc = results[0].get("product_code")
            result.top_score = results[0].get("score")

        if expected_type == "non_tls":
            # Pass: got normal results and the top SLC matches
            result.passed = (result.actual_slc == slc_code)
            if not result.passed:
                got_name = results[0].get("product_name", "?") if results else "NO RESULTS"
                result.error_msg = (
                    f"Wrong top result: got {result.actual_slc} ({got_name}) "
                    f"expected {slc_code}"
                )
        else:
            result.error_msg = (
                f"Expected TLS redirect but got normal results "
                f"(top={result.actual_slc}, score={result.top_score})"
            )

    else:
        result.actual_type = "no_results"
        result.error_msg = f"Unexpected response shape: {str(response)[:120]}"

    return result


def print_section(title: str, results: list[TestResult]):
    """Print a formatted section of results."""
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    win_rate = len(passed) / len(results) * 100 if results else 0

    status = "✅" if win_rate == 100 else ("⚠️" if win_rate >= 70 else "❌")
    print(f"\n{'─'*72}")
    print(f"  {status}  {title}")
    print(f"      Pass: {len(passed)}/{len(results)}  ({win_rate:.1f}%)")
    print(f"{'─'*72}")

    for r in results:
        icon = "✓" if r.passed else "✗"
        score_str = f"  score={r.top_score:.3f}" if r.top_score is not None else ""
        ms_str = f"  {r.execution_ms:.0f}ms"
        print(f"  {icon} [{r.slc_code:<12}] {r.product_name[:45]:<45}{score_str}{ms_str}")
        if not r.passed:
            print(f"      → {r.error_msg}")

    return len(passed), len(results)


def main():
    parser = argparse.ArgumentParser(description="TLS Routing Test Suite")
    parser.add_argument(
        "--base-url",
        default="https://cfai-product-catalog.2c0f20fvfl3u.us-south.codeengine.appdomain.cloud",
        help="Base URL of the API"
    )
    parser.add_argument("--verbose", action="store_true", help="Print raw responses")
    parser.add_argument("--output", help="Save JSON results to this file")
    parser.add_argument("--delay", type=float, default=0.1,
                        help="Seconds to wait between requests (default 0.1)")
    args = parser.parse_args()

    print(f"\n{'═'*72}")
    print(f"  TLS ROUTING TEST SUITE")
    print(f"  Base URL : {args.base_url}")
    print(f"  TLS cases: {len(TLS_PRODUCTS)}")
    print(f"  Non-TLS  : {len(NON_TLS_PRODUCTS)}")
    print(f"{'═'*72}")

    all_results: list[TestResult] = []

    # ── TLS products ──────────────────────────────────────────────────────
    print("\n  Running TLS product tests…", flush=True)
    tls_results = []
    for name, slc, assistant in TLS_PRODUCTS:
        r = run_test(args.base_url, name, slc, "tls", assistant, args.verbose)
        tls_results.append(r)
        all_results.append(r)
        dot = "." if r.passed else "F"
        print(dot, end="", flush=True)
        if args.verbose:
            print(f"\n  [{slc}] {name}")
            print(f"    passed={r.passed}  actual_type={r.actual_type}  actual_slc={r.actual_slc}")
            if r.error_msg:
                print(f"    error: {r.error_msg}")
        time.sleep(args.delay)

    # ── Non-TLS products ──────────────────────────────────────────────────
    print("\n  Running non-TLS product tests…", flush=True)
    non_tls_results = []
    for name, slc, assistant in NON_TLS_PRODUCTS:
        r = run_test(args.base_url, name, slc, "non_tls", assistant, args.verbose)
        non_tls_results.append(r)
        all_results.append(r)
        dot = "." if r.passed else "F"
        print(dot, end="", flush=True)
        if args.verbose:
            print(f"\n  [{slc}] {name}")
            print(f"    passed={r.passed}  actual_type={r.actual_type}  actual_slc={r.actual_slc}")
            if r.error_msg:
                print(f"    error: {r.error_msg}")
        time.sleep(args.delay)

    # ── Results ───────────────────────────────────────────────────────────
    print()
    tls_pass, tls_total       = print_section("TLS PRODUCTS — expect redirect", tls_results)
    non_tls_pass, non_tls_total = print_section("NON-TLS PRODUCTS — expect results", non_tls_results)

    total_pass  = tls_pass + non_tls_pass
    total       = tls_total + non_tls_total
    overall_pct = total_pass / total * 100 if total else 0

    print(f"\n{'═'*72}")
    print(f"  OVERALL SUMMARY")
    print(f"{'═'*72}")
    print(f"  TLS products      : {tls_pass:>3}/{tls_total:<3}  ({tls_pass/tls_total*100:.1f}%)")
    print(f"  Non-TLS products  : {non_tls_pass:>3}/{non_tls_total:<3}  ({non_tls_pass/non_tls_total*100:.1f}%)")
    print(f"  ─────────────────────────────")
    print(f"  TOTAL WIN RATE    : {total_pass:>3}/{total:<3}  ({overall_pct:.1f}%)")
    print(f"{'═'*72}")

    # Failures summary
    failures = [r for r in all_results if not r.passed]
    if failures:
        print(f"\n  FAILURES ({len(failures)})")
        print(f"  {'SLC':<12} {'Type':<8} {'Product':<45} Error")
        print(f"  {'─'*12} {'─'*8} {'─'*45} {'─'*30}")
        for r in failures:
            tag = "[TLS]    " if r.expected_type == "tls" else "[non-TLS]"
            print(f"  {r.slc_code:<12} {tag} {r.product_name[:44]:<45} {r.error_msg[:60]}")

    # Save JSON
    if args.output:
        output_data = {
            "base_url": args.base_url,
            "summary": {
                "tls_pass": tls_pass, "tls_total": tls_total,
                "non_tls_pass": non_tls_pass, "non_tls_total": non_tls_total,
                "total_pass": total_pass, "total": total,
                "overall_pct": round(overall_pct, 1),
            },
            "results": [
                {
                    "product_name": r.product_name,
                    "slc_code": r.slc_code,
                    "expected_type": r.expected_type,
                    "expected_assistant": r.expected_assistant,
                    "passed": r.passed,
                    "actual_type": r.actual_type,
                    "actual_slc": r.actual_slc,
                    "actual_assistant": r.actual_assistant,
                    "top_score": r.top_score,
                    "execution_ms": round(r.execution_ms, 1),
                    "error_msg": r.error_msg,
                }
                for r in all_results
            ]
        }
        with open(args.output, "w") as f:
            json.dump(output_data, f, indent=2)
        print(f"\n  Results saved to: {args.output}")

    print()
    sys.exit(0 if total_pass == total else 1)


if __name__ == "__main__":
    main()
