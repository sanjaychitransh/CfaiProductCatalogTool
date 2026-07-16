"""
TLS Routing Test Suite
======================
Tests the /products/search endpoint against known TLS and non-TLS products.

For TLS products   → expects { "tls_product": true, "slc_code": <exact>, "assistant": <exact> }
For non-TLS products → expects { "results": [...] } with the expected SLC at rank 1

Usage
-----
# Against local server (default)
python tests/test_tls_routing.py

# Against a different server
python tests/test_tls_routing.py --base-url http://localhost:8081

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

# (product_name, slc_code)
NON_TLS_PRODUCTS = [
    ("API Connect",                                      "SAIQ2"),
    ("Aspera",                                           "SAIT9"),
    ("Business Automation Workflow",                     "SAIN8"),
    ("CICS Transaction Server",                          "SCZJ2"),
    ("Cloud Pak for AIOps",                              "SAJE7"),
    ("Cloud Pak for Data",                               "SAAD9"),
    ("Cloud Pak for Integration",                        "SAJB2"),
    ("Cloudera Data Platform Private Cloud",             "SABE2"),
    ("Db2 for z/OS",                                     "SAAI8"),
    ("Db2 Linux, Unix and Windows",                      "SAAA0"),
    ("Engineering Requirements Management DOORS",        "SBIA7"),
    ("Engineering Workflow Management",                  "SBIA1"),
    ("Envizi ESG Suite",                                 "SBW03"),
    ("FileNet Content Manager",                          "SAIL4"),
    ("Financial Transaction Manager",                    "SBFF3"),
    ("Guardium Data Protection",                         "SBSE2"),
    ("IMS",                                              "SCZA1"),
    ("Informix Dynamic Server",                          "SAAG3"),
    ("Instana Observability",                            "SAJG6"),
    ("Jazz for Service Management DASH / TIP",           "SAID8"),
    ("MaaS360",                                          "SBSH2"),
    ("Maximo Asset Management",                          "SBIF8"),
    ("Maximo Application Suite",                         "SBIK2"),
    ("Netcool/OMNIbus",                                  "SAIC0"),
    ("OpenPages",                                        "SBFD6"),
    ("QRadar SIEM",                                      "SBSC3"),
    ("SPSS Statistics",                                  "SAAA9"),
    ("Safer Payments",                                   "SBFE4"),
    ("Sterling Order Management",                        "SBWI5"),
    ("Turbonomic On-Premises",                           "SAJD9"),
    ("Verify Identity Access",                           "SBSD1"),
    ("WebSphere Application Server",                     "SAIM8"),
    ("Workload Scheduler",                               "SAIP6"),
    ("Spectrum Control",                                 "SCSA5"),
    ("DevOps Deploy",                                    "SAIT2"),
    ("DataPower",                                        "SAII7"),
    ("Operational Decision Manager",                     "SAIP4"),
    ("NS1 Connect",                                      "SBFI5"),
    ("SevOne Network Performance Management",            "SAJG5"),
    ("InfoSphere Information Server: Data Integration",  "SAAD0"),
]


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    product_name: str
    slc_code: str
    expected_type: str          # "tls" | "non_tls"
    query: str
    passed: bool
    actual_type: str            # "tls" | "non_tls" | "error" | "no_results"
    actual_slc: Optional[str]
    actual_assistant: Optional[str]
    top_score: Optional[float]
    execution_ms: float
    error_msg: str = ""
    pass_reason: str = ""
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


def run_tls_test(base_url: str, product_name: str, slc_code: str,
                 expected_assistant: str) -> TestResult:

    query = product_name
    response, elapsed = call_api(base_url, query)

    result = TestResult(
        product_name=product_name,
        slc_code=slc_code,
        expected_type="tls",
        query=query,
        passed=False,
        actual_type="error",
        actual_slc=None,
        actual_assistant=None,
        top_score=None,
        execution_ms=elapsed,
        raw_response=response,
    )

    if "_exception" in response:
        result.error_msg = response["_exception"]
        return result
    if "_http_error" in response:
        result.error_msg = f"HTTP {response['_http_error']}: {response.get('_body','')[:120]}"
        return result

    if response.get("tls_product") is True:
        result.actual_type = "tls"
        result.actual_slc = response.get("slc_code")
        result.actual_assistant = response.get("assistant")

        slc_match = (result.actual_slc == slc_code)
        assistant_match = (result.actual_assistant == expected_assistant)

        if slc_match and assistant_match:
            result.passed = True
            result.pass_reason = "exact SLC + exact assistant"
        elif not slc_match:
            result.error_msg = (
                f"Wrong SLC: got '{result.actual_slc}' expected '{slc_code}' "
                f"(assistant={result.actual_assistant})"
            )
        else:
            result.error_msg = (
                f"Wrong assistant: got '{result.actual_assistant}' "
                f"expected '{expected_assistant}' (slc={result.actual_slc})"
            )

    elif "results" in response:
        result.actual_type = "non_tls"
        results_list = response["results"]
        if results_list:
            result.actual_slc = results_list[0].get("product_code")
            result.top_score = results_list[0].get("score")
        result.error_msg = (
            f"Expected TLS redirect but got normal results "
            f"(top={result.actual_slc}, score={result.top_score})"
        )

    else:
        result.actual_type = "no_results"
        result.error_msg = f"Unexpected response shape: {str(response)[:120]}"

    return result


def run_non_tls_test(base_url: str, product_name: str, slc_code: str) -> TestResult:

    query = product_name
    response, elapsed = call_api(base_url, query)

    result = TestResult(
        product_name=product_name,
        slc_code=slc_code,
        expected_type="non_tls",
        query=query,
        passed=False,
        actual_type="error",
        actual_slc=None,
        actual_assistant=None,
        top_score=None,
        execution_ms=elapsed,
        raw_response=response,
    )

    if "_exception" in response:
        result.error_msg = response["_exception"]
        return result
    if "_http_error" in response:
        result.error_msg = f"HTTP {response['_http_error']}: {response.get('_body','')[:120]}"
        return result

    if response.get("tls_product") is True:
        result.actual_type = "tls"
        result.actual_slc = response.get("slc_code")
        result.actual_assistant = response.get("assistant")
        result.error_msg = (
            f"Expected normal result but got TLS redirect "
            f"(slc={result.actual_slc}, assistant={result.actual_assistant})"
        )

    elif "results" in response:
        results_list = response["results"]
        result.actual_type = "non_tls"

        if results_list:
            result.actual_slc = results_list[0].get("product_code")
            result.top_score = results_list[0].get("score")

        # PASS rule: expected SLC must be rank-1
        if result.actual_slc == slc_code:
            result.passed = True
            result.pass_reason = "rank-1 exact SLC"
        else:
            got_name = results_list[0].get("product_name", "?") if results_list else "NO RESULTS"
            result.error_msg = (
                f"Wrong rank-1: got {result.actual_slc} ({got_name}), "
                f"expected {slc_code}"
            )

    else:
        result.actual_type = "no_results"
        result.error_msg = f"Unexpected response shape: {str(response)[:120]}"

    return result


def print_section(title: str, results: list[TestResult]):
    passed = [r for r in results if r.passed]
    win_rate = len(passed) / len(results) * 100 if results else 0

    status = "✅" if win_rate == 100 else ("⚠️" if win_rate >= 70 else "❌")
    print(f"\n{'─'*72}")
    print(f"  {status}  {title}")
    print(f"      Pass: {len(passed)}/{len(results)}  ({win_rate:.1f}%)")
    print("─"*72)

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
        default="http://localhost:8081",
        help="Base URL of the API (default: http://localhost:8081)"
    )
    parser.add_argument("--verbose", action="store_true", help="Print raw responses")
    parser.add_argument("--output", help="Save JSON results to this file")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds to wait between requests (default 0.0)")
    args = parser.parse_args()

    print(f"\n{'═'*72}")
    print(f"  TLS ROUTING TEST SUITE  (strict — no relaxations)")
    print(f"  Base URL : {args.base_url}")
    print(f"  TLS cases: {len(TLS_PRODUCTS)}  (exact SLC + exact assistant required)")
    print(f"  Non-TLS  : {len(NON_TLS_PRODUCTS)}  (rank-1 exact SLC required)")
    print(f"{'═'*72}")

    all_results: list[TestResult] = []

    # ── TLS products ──────────────────────────────────────────────────────
    print("\n  Running TLS product tests…", flush=True)
    tls_results = []
    for name, slc, assistant in TLS_PRODUCTS:
        r = run_tls_test(args.base_url, name, slc, assistant)
        tls_results.append(r)
        all_results.append(r)
        print("." if r.passed else "F", end="", flush=True)
        if args.verbose:
            print(f"\n  [{slc}] {name}")
            print(f"    passed={r.passed}  actual_slc={r.actual_slc}  actual_assistant={r.actual_assistant}")
            if r.error_msg:
                print(f"    error: {r.error_msg}")
        if args.delay:
            time.sleep(args.delay)

    # ── Non-TLS products ──────────────────────────────────────────────────
    print("\n  Running non-TLS product tests…", flush=True)
    non_tls_results = []
    for name, slc in NON_TLS_PRODUCTS:
        r = run_non_tls_test(args.base_url, name, slc)
        non_tls_results.append(r)
        all_results.append(r)
        print("." if r.passed else "F", end="", flush=True)
        if args.verbose:
            print(f"\n  [{slc}] {name}")
            print(f"    passed={r.passed}  actual_slc={r.actual_slc}  score={r.top_score}")
            if r.error_msg:
                print(f"    error: {r.error_msg}")
        if args.delay:
            time.sleep(args.delay)

    # ── Results ───────────────────────────────────────────────────────────
    print()
    tls_pass, tls_total         = print_section("TLS PRODUCTS — expect exact redirect", tls_results)
    non_tls_pass, non_tls_total = print_section("NON-TLS PRODUCTS — expect rank-1 result", non_tls_results)

    total_pass  = tls_pass + non_tls_pass
    total       = tls_total + non_tls_total
    overall_pct = total_pass / total * 100 if total else 0

    print(f"\n{'═'*72}")
    print("  OVERALL SUMMARY  (strict)")
    print(f"{'═'*72}")
    print(f"  TLS products      : {tls_pass:>3}/{tls_total:<3}  ({tls_pass/tls_total*100:.1f}%)")
    print(f"  Non-TLS products  : {non_tls_pass:>3}/{non_tls_total:<3}  ({non_tls_pass/non_tls_total*100:.1f}%)")
    print("  " + "─"*29)
    print(f"  TOTAL WIN RATE    : {total_pass:>3}/{total:<3}  ({overall_pct:.1f}%)")
    print(f"{'═'*72}")

    failures = [r for r in all_results if not r.passed]
    if failures:
        print(f"\n  FAILURES ({len(failures)})")
        print(f"  {'SLC':<12} {'Type':<9} {'Product':<45} Error")
        print("  " + "─"*12 + " " + "─"*9 + " " + "─"*45 + " " + "─"*35)
        for r in failures:
            tag = "[TLS]    " if r.expected_type == "tls" else "[non-TLS]"
            print(f"  {r.slc_code:<12} {tag} {r.product_name[:44]:<45} {r.error_msg[:60]}")

    if args.output:
        output_data = {
            "base_url": args.base_url,
            "strict": True,
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
