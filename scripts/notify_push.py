"""
Send the weekly digest as a Web Push notification.

Run by the GitHub Action after the overseer finishes. The Action is the
"server" that sends the push — GitHub Pages itself can't. No-ops cleanly if
push isn't set up yet, so the workflow never fails over a missing secret.

Required environment (set as repo secrets) to actually send:
  VAPID_PRIVATE_KEY   private key from `web-push generate-vapid-keys`
  VAPID_SUBJECT       a contact URI, e.g. mailto:you@example.com
  PUSH_SUBSCRIPTION   the subscription JSON the dashboard showed you
                      (a single object, or a JSON array for multiple devices)
"""

import json
import os
import sys

DIGEST_PATH = os.getenv("DIGEST_PATH", "docs/digest.json")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", ".")


def main() -> int:
    private_key = os.getenv("VAPID_PRIVATE_KEY")
    subject = os.getenv("VAPID_SUBJECT")
    raw_sub = os.getenv("PUSH_SUBSCRIPTION")
    if not (private_key and subject and raw_sub):
        print("[push] not configured (VAPID_PRIVATE_KEY / VAPID_SUBJECT / PUSH_SUBSCRIPTION) — skipping.")
        return 0

    try:
        with open(DIGEST_PATH, encoding="utf-8") as f:
            digest = json.load(f)
    except FileNotFoundError:
        print(f"[push] {DIGEST_PATH} not found — skipping.")
        return 0

    counts = digest.get("counts", {})
    body = (
        f"{counts.get('issues', 0)} issue(s), {counts.get('enhancements', 0)} enhancement(s)"
        f"{', ' + str(counts['errors']) + ' error(s)' if counts.get('errors') else ''}."
        " Tap to read the digest."
    )
    payload = json.dumps({"title": "Weekly review ready", "body": body, "url": DASHBOARD_URL})

    subs = json.loads(raw_sub)
    if isinstance(subs, dict):
        subs = [subs]

    from pywebpush import WebPushException, webpush

    sent = 0
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},
            )
            sent += 1
        except WebPushException as exc:
            # 404/410 means the device unsubscribed — log and continue.
            print(f"[push] failed for one subscription: {exc}")
    print(f"[push] sent to {sent}/{len(subs)} subscription(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
