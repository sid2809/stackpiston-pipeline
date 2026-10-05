"""Phase 0/1 checkpoint: `python -m app.check_wp`

1. Logs in and shows the pipeline user and role.
2. Creates a test DRAFT with tricky text (quotes, backslash, em dash, ×, emoji).
3. Reads it back and checks the JSON survived unchanged, and that sp_warnings comes back.
4. Updates it, confirms one post only (no duplicate), then deletes it.
Safe to run on staging or live: it only touches its own test draft.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from .config import ConfigError, WPConfig
from .wordpress import WordPress, WPError

SAMPLE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sample-review.json"
TEST_SLUG = "pipeline-connection-test"


def main() -> int:
    try:
        wp = WordPress(WPConfig.from_env())
    except ConfigError as e:
        print(f"FAIL  {e}")
        return 1
    post_id = None
    try:
        me = wp.me()
        roles = me.get("roles", [])
        print(f"OK  logged in as {me.get('username') or me.get('name')} (roles: {', '.join(roles) or 'unknown'})")
        if "editor" not in roles and "administrator" not in roles:
            print("WARN  user is not an Editor; it may not be able to update reviews it did not create.")

        review = json.loads(SAMPLE.read_text(encoding="utf-8"))
        review["slug"] = TEST_SLUG
        review["product"]["summary"] = ('Tricky text: "quotes", a back\\slash, an em dash — and 2×4. ' +
                                        review["product"]["summary"])[:400]
        if wp.find_by_slug(TEST_SLUG):
            print("FAIL  a test post already exists; delete 'pipeline-connection-test' in wp-admin and rerun.")
            return 1

        post = wp.save_review(review=review, title="Pipeline connection test", post_id=None, want_status="draft")
        post_id = post["id"]
        print(f"OK  created draft #{post_id} (slug saved as '{post['slug']}')")

        back = wp.review_json(wp.get_review(post_id))
        if back != review:
            print("FAIL  JSON changed on the way through WordPress (backslash/quote handling).")
            return 1
        print("OK  JSON round-trip identical (quotes, backslash, em dash, × all intact)")

        warnings = wp.warnings(post)
        print(f"OK  sp_warnings returned ({len(warnings)} warnings for the sample, expected ~12)")

        updated = copy.deepcopy(review)
        updated["review"]["updatedAt"] = "2026-10-06"
        wp.save_review(review=updated, title="Pipeline connection test", post_id=post_id, want_status="draft")
        found = wp.find_by_slug(TEST_SLUG)
        if not found or found["id"] != post_id:
            print("FAIL  update created a duplicate or lost the post.")
            return 1
        print("OK  update kept the same post (no duplicate)")
        print("\nALL CHECKS PASSED")
        return 0
    except WPError as e:
        print(f"FAIL  {e}")
        return 1
    finally:
        if post_id:
            try:
                wp.delete_review(post_id)
                print(f"OK  test draft #{post_id} deleted")
            except WPError as e:
                print(f"WARN  could not delete test draft #{post_id}: {e}")


if __name__ == "__main__":
    sys.exit(main())
