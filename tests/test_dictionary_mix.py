"""
Dictionary-Mix Test Suite (Set 2)
==================================
Complements test_tls_routing.py with a second set of cases drawn directly
from product_match_dictionary.json.

Key differences from set 1:
- TLS products are queried via **alternative aliases** (not the canonical product
  name) — exercises synonym paths, model numbers, and variant names.
- Non-TLS products are **new SLCs** not covered in set 1.

Pass rules (same as set 1 — strict, no relaxations):
- TLS   → exact SLC + exact assistant in the TLS redirect response.
- Non-TLS → expected SLC at rank-1 in the results list.

Usage
-----
# Against local server (default: http://localhost:8081)
python tests/test_dictionary_mix.py

# Against a different server
python tests/test_dictionary_mix.py --base-url http://localhost:8080

# Verbose
python tests/test_dictionary_mix.py --verbose

# Save JSON
python tests/test_dictionary_mix.py --output mix_results.json
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
# Test data
# ---------------------------------------------------------------------------

# TLS products queried by alternative / synonym aliases pulled from the
# fuzzy_match section of product_match_dictionary.json.
# (query_alias, slc_code, assistant)
TLS_PRODUCTS = [
    # AIX / IBM i / HMC
    ("as/400 operating system",                       "SCPF9",   "IBM i (Support)"),         # IBM i
    ("i series",                                       "SCPF9",   "IBM i (Support)"),          # IBM i
    ("ibm i on power virtual server",                  "SCPMH",   "IBM i (Support)"),          # IBM i on PowerVS
    ("suse linux enterprise server for sap applications", "SCPG7", "MVS Linux (Support)"),     # SUSE for SAP
    ("red hat enterprise linux for sap solutions",     "SCPK2",   "MVS Linux (Support)"),      # RHEL for SAP
    ("red hat openstack platform",                     "SCPK6",   "MVS Linux (Support)"),      # Red Hat OpenStack
    # Cisco
    ("asa series",                                     "SMC0A12", "MVS CISCO Network (Support)"),  # ASA Series
    ("cisco aironet network access point",             "SMC0A04", "MVS CISCO Network (Support)"),  # ACE
    # Power Servers
    ("power system s822 server",                       "SIP0B43", "Power Servers (Support)"),
    ("power system e870 server",                       "SIP0B4B", "Power Servers (Support)"),
    ("power system e950 server",                       "SIP0B4M", "Power Servers (Support)"),
    ("power system s1124 server",                      "SIP0B5I", "Power Servers (Support)"),
    ("power system l1024 server",                      "SIP0B69", "Power Servers (Support)"),
    # Storage / SAN
    ("ds8000 legacy systems",                          "SIS0A10", "DS8000 (Support)"),
    ("san c-type collection",                          "SIS0A53", "SAN (Support)"),
    # Tape Storage
    ("tape drive ts1160",                              "SIS0A90", "Tape Storage (Support)"),
    ("tape drive ts1090",                              "SIS0ACA", "Tape Storage (Support)"),
    ("spectrum archive enterprise and library",        "SIS0AB6", "Tape Storage (Support)"),
    # z/OS
    ("python ai toolkit for z/os",                     "SCZXV",   "z/OS (Support)"),
    ("zcx foundation for red hat openshift for z/os",  "SSZA3",   "z/OS (Support)"),
]

# Non-TLS products — new SLCs not present in test set 1.
# (query_alias, slc_code)
NON_TLS_PRODUCTS = [
    ("doors ng",                                       "SBIA3"),   # Engineering Requirements Management DOORS Next
    ("guardium insights",                              "SBSHB"),   # Guardium Insights
    ("infosphere data architect",                      "SAAF5"),   # InfoSphere Data Architect
    ("db2 administration solution pack for z/os",      "SAALR"),   # Db2 Administration Solution Pack for z/OS
    ("devops test embedded",                           "SAIR0"),   # DevOps Test Embedded
    ("gitlab premium for z",                           "SABH0"),   # GitLab Premium for IBM Z
    ("z open development",                             "SCZWM"),   # z Open Development
    ("cics vt",                                        "SCZU0"),   # CICS VSAM Transparency
    ("overlay generation language 370",                "SCPLF"),   # Overlay Generation Language/370
    ("filenet image services connector",               "SAIL8"),   # FileNet Image Services Connector
    ("order optimizer",                                "SBWH1"),   # Sterling Fulfillment Optimizer with Watson
    ("enterprise application runtimes",                "SAJIZ"),   # Enterprise Application Runtimes
    ("power system 795 server",                        "SIP0B31"), # Power System 795 Server
    ("string transfer utility",                        "SBFI2"),   # String Transfer Utility
    ("sourcewise data collector",                      "SIN0A01"), # Sourcewise Data Collector
    ("network finance workstation display",            "SIT0A01"), # Network Finance WorkStation Display
]


# ---------------------------------------------------------------------------
# Test runner (identical logic to test_tls_routing.py)
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    product_name: str
    slc_code: str
    expected_type: str
    query: str
    passed: bool
    actual_type: str
    actual_slc: Optional[str]
    actual_assistant: Optional[str]
    top_score: Optional[float]
    execution_ms: float
    error_msg: str = ""
    pass_reason: str = ""
    raw_response: dict = field(default_factory=dict)


def call_api(base_url: str, query: str, timeout: int = 15) -> tuple[dict, float]:
    params = urllib.parse.urlencode({"query": query, "limit": 10, "threshold": 0.70})
    url = f"{base_url.rstrip('/')}/products/search?{params}"
    t0 = time.time()
    try:
        req = urllib.request.urlopen(url, timeout=timeout)
        body = req.read().decode("utf-8")
        return json.loads(body), (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return {"_http_error": e.code, "_body": body}, (time.time() - t0) * 1000
    except Exception as e:
        return {"_exception": str(e)}, (time.time() - t0) * 1000


def run_tls_test(base_url: str, query: str, slc_code: str,
                 expected_assistant: str) -> TestResult:
    response, elapsed = call_api(base_url, query)
    result = TestResult(
        product_name=query, slc_code=slc_code, expected_type="tls",
        query=query, passed=False, actual_type="error",
        actual_slc=None, actual_assistant=None, top_score=None,
        execution_ms=elapsed, raw_response=response,
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
        if result.actual_slc == slc_code and result.actual_assistant == expected_assistant:
            result.passed = True
            result.pass_reason = "exact SLC + exact assistant"
        elif result.actual_slc != slc_code:
            result.error_msg = (
                f"Wrong SLC: got '{result.actual_slc}' expected '{slc_code}' "
                f"(assistant={result.actual_assistant})"
            )
        else:
            result.error_msg = (
                f"Wrong assistant: got '{result.actual_assistant}' "
                f"expected '{expected_assistant}'"
            )
    elif "results" in response:
        result.actual_type = "non_tls"
        rl = response["results"]
        if rl:
            result.actual_slc = rl[0].get("product_code")
            result.top_score = rl[0].get("score")
        result.error_msg = (
            f"Expected TLS redirect but got normal results "
            f"(top={result.actual_slc}, score={result.top_score})"
        )
    else:
        result.actual_type = "no_results"
        result.error_msg = f"Unexpected response shape: {str(response)[:120]}"
    return result


def run_non_tls_test(base_url: str, query: str, slc_code: str) -> TestResult:
    response, elapsed = call_api(base_url, query)
    result = TestResult(
        product_name=query, slc_code=slc_code, expected_type="non_tls",
        query=query, passed=False, actual_type="error",
        actual_slc=None, actual_assistant=None, top_score=None,
        execution_ms=elapsed, raw_response=response,
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
        rl = response["results"]
        result.actual_type = "non_tls"
        if rl:
            result.actual_slc = rl[0].get("product_code")
            result.top_score = rl[0].get("score")
        if result.actual_slc == slc_code:
            result.passed = True
            result.pass_reason = "rank-1 exact SLC"
        else:
            got_name = rl[0].get("product_name", "?") if rl else "NO RESULTS"
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
        print(f"  {icon} [{r.slc_code:<12}] {r.query[:45]:<45}{score_str}  {r.execution_ms:.0f}ms")
        if not r.passed:
            print(f"      → {r.error_msg}")
    return len(passed), len(results)


def main():
    parser = argparse.ArgumentParser(description="Dictionary-Mix Test Suite (Set 2)")
    parser.add_argument("--base-url", default="http://localhost:8081",
                        help="Base URL of the API (default: http://localhost:8081)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", help="Save JSON results to this file")
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args()

    print(f"\n{'═'*72}")
    print("  DICTIONARY-MIX TEST SUITE  (Set 2 — strict)")
    print(f"  Base URL : {args.base_url}")
    print(f"  TLS cases: {len(TLS_PRODUCTS)}  (alternative aliases from dictionary)")
    print(f"  Non-TLS  : {len(NON_TLS_PRODUCTS)}  (new SLCs from dictionary)")
    print(f"{'═'*72}")

    all_results: list[TestResult] = []

    print("\n  Running TLS product tests…", flush=True)
    tls_results = []
    for query, slc, assistant in TLS_PRODUCTS:
        r = run_tls_test(args.base_url, query, slc, assistant)
        tls_results.append(r)
        all_results.append(r)
        print("." if r.passed else "F", end="", flush=True)
        if args.verbose:
            print(f"\n  [{slc}] {query}")
            print(f"    passed={r.passed}  actual_slc={r.actual_slc}  actual_assistant={r.actual_assistant}")
            if r.error_msg:
                print(f"    error: {r.error_msg}")
        if args.delay:
            time.sleep(args.delay)

    print("\n  Running non-TLS product tests…", flush=True)
    non_tls_results = []
    for query, slc in NON_TLS_PRODUCTS:
        r = run_non_tls_test(args.base_url, query, slc)
        non_tls_results.append(r)
        all_results.append(r)
        print("." if r.passed else "F", end="", flush=True)
        if args.verbose:
            print(f"\n  [{slc}] {query}")
            print(f"    passed={r.passed}  actual_slc={r.actual_slc}  score={r.top_score}")
            if r.error_msg:
                print(f"    error: {r.error_msg}")
        if args.delay:
            time.sleep(args.delay)

    print()
    tls_pass, tls_total         = print_section("TLS PRODUCTS — alternative aliases", tls_results)
    non_tls_pass, non_tls_total = print_section("NON-TLS PRODUCTS — new SLCs", non_tls_results)

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
        print(f"  {'SLC':<12} {'Type':<9} {'Query':<45} Error")
        print("  " + "─"*12 + " " + "─"*9 + " " + "─"*45 + " " + "─"*35)
        for r in failures:
            tag = "[TLS]    " if r.expected_type == "tls" else "[non-TLS]"
            print(f"  {r.slc_code:<12} {tag} {r.query[:44]:<45} {r.error_msg[:60]}")

    if args.output:
        output_data = {
            "base_url": args.base_url,
            "suite": "dictionary-mix",
            "strict": True,
            "summary": {
                "tls_pass": tls_pass, "tls_total": tls_total,
                "non_tls_pass": non_tls_pass, "non_tls_total": non_tls_total,
                "total_pass": total_pass, "total": total,
                "overall_pct": round(overall_pct, 1),
            },
            "results": [
                {
                    "query": r.query,
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
