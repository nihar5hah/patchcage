from __future__ import annotations

import argparse
import json
import sys

from patchcage.sandbox.image import IMAGE_TAG, build_runtime_image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    image_id = build_runtime_image()
    payload = {"image": IMAGE_TAG, "image_id": image_id}
    if args.json:
        print(json.dumps(payload))
    else:
        print(f"{IMAGE_TAG} {image_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
