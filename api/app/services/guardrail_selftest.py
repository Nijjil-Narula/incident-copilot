"""
api/app/services/guardrail_selftest.py

Standalone verification test suite for the Whole-Command Matching Guardrail.

Runnable inside the Docker container with:
  python -m app.services.guardrail_selftest

Covers all 14 mandated test cases:
   1 verbatim runbook command passes
   2 same command with one changed flag fails
   3 whitespace differences pass
   4 command only in a POSTMORTEM chunk fails
   5 backslash-continued multi-line command passes
   6 comment lines inside code blocks are ignored
   7 "$ " prefix handled
   8 inline span that is not a command (`Connection Pool Exhausted`) is ignored
   9 truncated command: candidate "rm -rf /" against runbook containing "rm -rf /tmp/cache" is UNVERIFIED
  10 a leading "sudo" dropped from a runbook command is tolerated
  11 a prose list item "1. kubectl delete pod x" is detected as a candidate and is unverified
  12 a prose sentence that merely contains the word "delete" is ignored
  13 runbook "docker exec app rm -rf /tmp/cache", candidate "rm -rf /tmp/cache" is UNVERIFIED
  14 runbook "systemctl restart app", candidate "sudo systemctl restart app" is UNVERIFIED
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from app.services.guardrail import verify_guardrail


@dataclass
class TestChunk:
    doc_type: str
    text: str


def run_tests() -> bool:
    """Runs all 12 guardrail verification test cases and prints status."""
    all_passed = True

    def record_result(name: str, passed: bool, detail: str = ""):
        nonlocal all_passed
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"[{status}] {name}{f': {detail}' if detail else ''}")

    print("Running Guardrail Self-Test Suite...\n" + "=" * 50)

    # 1. Verbatim runbook command passes
    try:
        rb = TestChunk("runbook", "```bash\nkubectl get pods -n production\n```")
        ans = "Run the following:\n```bash\nkubectl get pods -n production\n```"
        res = verify_guardrail(ans, [rb])
        passed = res.guardrail == "pass" and len(res.unverified_commands) == 0
        record_result("Test 1: verbatim runbook command passes", passed)
    except Exception as exc:
        record_result("Test 1: verbatim runbook command passes", False, str(exc))

    # 2. Same command with one changed flag fails
    try:
        rb = TestChunk("runbook", "```bash\nkubectl get pods -n production\n```")
        ans = "Run the following:\n```bash\nkubectl get pods -n staging\n```"
        res = verify_guardrail(ans, [rb])
        passed = (
            res.guardrail == "redacted"
            and "kubectl get pods -n staging" in res.unverified_commands
            and "[UNVERIFIED COMMAND REMOVED]" in res.text
        )
        record_result("Test 2: same command with one changed flag fails", passed)
    except Exception as exc:
        record_result("Test 2: same command with one changed flag fails", False, str(exc))

    # 3. Whitespace differences pass
    try:
        rb = TestChunk("runbook", "```bash\nkubectl   get   pods    -n production\n```")
        ans = "```bash\nkubectl get pods -n production\n```"
        res = verify_guardrail(ans, [rb])
        passed = res.guardrail == "pass" and len(res.unverified_commands) == 0
        record_result("Test 3: whitespace differences pass", passed)
    except Exception as exc:
        record_result("Test 3: whitespace differences pass", False, str(exc))

    # 4. Command only in a POSTMORTEM chunk fails
    try:
        pm = TestChunk("postmortem", "```bash\nkubectl delete pod bad-pod-123\n```")
        ans = "```bash\nkubectl delete pod bad-pod-123\n```"
        res = verify_guardrail(ans, [pm])
        passed = (
            res.guardrail == "redacted"
            and "kubectl delete pod bad-pod-123" in res.unverified_commands
            and "[UNVERIFIED COMMAND REMOVED]" in res.text
        )
        record_result("Test 4: command only in a POSTMORTEM chunk fails", passed)
    except Exception as exc:
        record_result("Test 4: command only in a POSTMORTEM chunk fails", False, str(exc))

    # 5. Backslash-continued multi-line command passes
    try:
        rb = TestChunk(
            "runbook",
            "```bash\ndocker run -d \\\n  --name redis \\\n  -p 6379:6379 \\\n  redis:alpine\n```",
        )
        ans = (
            "Start redis:\n```bash\ndocker run -d \\\n  --name redis \\\n  -p 6379:6379 \\\n  redis:alpine\n```"
        )
        res = verify_guardrail(ans, [rb])
        passed = res.guardrail == "pass" and len(res.unverified_commands) == 0
        record_result("Test 5: backslash-continued multi-line command passes", passed)
    except Exception as exc:
        record_result("Test 5: backslash-continued multi-line command passes", False, str(exc))

    # 6. Comment lines inside code blocks are ignored
    try:
        rb = TestChunk("runbook", "```bash\nsystemctl restart nginx\n```")
        ans = "```bash\n# restart nginx service\nsystemctl restart nginx\n```"
        res = verify_guardrail(ans, [rb])
        passed = (
            res.guardrail == "pass"
            and len(res.unverified_commands) == 0
            and "# restart nginx service" in res.text
        )
        record_result("Test 6: comment lines inside code blocks are ignored", passed)
    except Exception as exc:
        record_result("Test 6: comment lines inside code blocks are ignored", False, str(exc))

    # 7. "$ " prefix handled
    try:
        rb = TestChunk("runbook", "```bash\n$ kubectl get services\n```")
        ans = "```bash\n$ kubectl get services\n```"
        res = verify_guardrail(ans, [rb])
        passed = res.guardrail == "pass" and len(res.unverified_commands) == 0
        record_result("Test 7: '$ ' prefix handled", passed)
    except Exception as exc:
        record_result("Test 7: '$ ' prefix handled", False, str(exc))

    # 8. Inline span that is not a command (`Connection Pool Exhausted`) is ignored
    try:
        rb = TestChunk("runbook", "Incident triage guide for database issues.")
        ans = "Observed status: `Connection Pool Exhausted` in primary database."
        res = verify_guardrail(ans, [rb])
        passed = (
            res.guardrail == "pass"
            and len(res.unverified_commands) == 0
            and "`Connection Pool Exhausted`" in res.text
        )
        record_result("Test 8: inline span that is not a command (`Connection Pool Exhausted`) is ignored", passed)
    except Exception as exc:
        record_result("Test 8: inline span that is not a command (`Connection Pool Exhausted`) is ignored", False, str(exc))

    # 9. Truncated command: candidate "rm -rf /" against runbook containing "rm -rf /tmp/cache" is UNVERIFIED
    try:
        rb = TestChunk("runbook", "```bash\nrm -rf /tmp/cache\n```")
        ans = "Run cleanup:\n```bash\nrm -rf /\n```"
        res = verify_guardrail(ans, [rb])
        passed = (
            res.guardrail == "redacted"
            and "rm -rf /" in res.unverified_commands
            and "[UNVERIFIED COMMAND REMOVED]" in res.text
            and "rm -rf" in res.destructive_commands
        )
        record_result("Test 9: truncated command: candidate 'rm -rf /' against runbook containing 'rm -rf /tmp/cache' is UNVERIFIED", passed)
    except Exception as exc:
        record_result("Test 9: truncated command: candidate 'rm -rf /' against runbook containing 'rm -rf /tmp/cache' is UNVERIFIED", False, str(exc))

    # 10. A leading "sudo" dropped from a runbook command is tolerated
    try:
        rb = TestChunk("runbook", "```bash\nsudo systemctl restart app\n```")
        ans = "Restart the app:\n```bash\nsystemctl restart app\n```"
        res = verify_guardrail(ans, [rb])
        passed = (
            res.guardrail == "pass"
            and len(res.unverified_commands) == 0
            and "systemctl restart app" in res.text
        )
        record_result("Test 10: a leading 'sudo' dropped from a runbook command is tolerated", passed)
    except Exception as exc:
        record_result("Test 10: a leading 'sudo' dropped from a runbook command is tolerated", False, str(exc))

    # 11. A prose list item "1. kubectl delete pod x" is detected as a candidate and is unverified
    try:
        rb = TestChunk("runbook", "General maintenance operations.")
        ans = "Follow these steps:\n1. kubectl delete pod x\nCheck logs."
        res = verify_guardrail(ans, [rb])
        passed = (
            res.guardrail == "redacted"
            and "kubectl delete pod x" in res.unverified_commands
            and "[UNVERIFIED COMMAND REMOVED]" in res.text
        )
        record_result("Test 11: a prose list item '1. kubectl delete pod x' is detected as a candidate and is unverified", passed)
    except Exception as exc:
        record_result("Test 11: a prose list item '1. kubectl delete pod x' is detected as a candidate and is unverified", False, str(exc))

    # 12. A prose sentence that merely contains the word "delete" is ignored
    try:
        rb = TestChunk("runbook", "General maintenance operations.")
        ans = "You can delete old logs later."
        res = verify_guardrail(ans, [rb])
        passed = (
            res.guardrail == "pass"
            and len(res.unverified_commands) == 0
            and "You can delete old logs later." in res.text
        )
        record_result("Test 12: a prose sentence that merely contains the word 'delete' is ignored", passed)
    except Exception as exc:
        record_result("Test 12: a prose sentence that merely contains the word 'delete' is ignored", False, str(exc))

    # 13. Suffix dropping not allowed: runbook "docker exec app rm -rf /tmp/cache", candidate "rm -rf /tmp/cache" is UNVERIFIED
    try:
        rb = TestChunk("runbook", "```bash\ndocker exec app rm -rf /tmp/cache\n```")
        ans = "Run cleanup:\n```bash\nrm -rf /tmp/cache\n```"
        res = verify_guardrail(ans, [rb])
        passed = (
            res.guardrail == "redacted"
            and "rm -rf /tmp/cache" in res.unverified_commands
            and "[UNVERIFIED COMMAND REMOVED]" in res.text
        )
        record_result("Test 13: runbook 'docker exec app rm -rf /tmp/cache', candidate 'rm -rf /tmp/cache' is UNVERIFIED", passed)
    except Exception as exc:
        record_result("Test 13: runbook 'docker exec app rm -rf /tmp/cache', candidate 'rm -rf /tmp/cache' is UNVERIFIED", False, str(exc))

    # 14. Adding sudo to runbook without sudo is UNVERIFIED
    try:
        rb = TestChunk("runbook", "```bash\nsystemctl restart app\n```")
        ans = "Restart service:\n```bash\nsudo systemctl restart app\n```"
        res = verify_guardrail(ans, [rb])
        passed = (
            res.guardrail == "redacted"
            and "sudo systemctl restart app" in res.unverified_commands
            and "[UNVERIFIED COMMAND REMOVED]" in res.text
        )
        record_result("Test 14: runbook 'systemctl restart app', candidate 'sudo systemctl restart app' is UNVERIFIED", passed)
    except Exception as exc:
        record_result("Test 14: runbook 'systemctl restart app', candidate 'sudo systemctl restart app' is UNVERIFIED", False, str(exc))

    print("=" * 50)
    if all_passed:
        print("ALL 14 TESTS PASSED.")
        return True
    else:
        print("SOME TESTS FAILED.")
        return False


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
