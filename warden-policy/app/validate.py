"""Checks a policy folder without serving it (scenarios 06 and 07).

    python -m app.validate /policies
Exit 0 accepted, 1 rejected."""
import sys

from .policyset import MATRIX, Rejected, load


def main(argv: list[str]) -> int:
    directory = argv[1] if len(argv) > 1 else "/policies"
    try:
        policy_set = load(directory)
    except Rejected as e:
        print(f"REJECTED: {directory}")
        for p in e.problems:
            print(f"  - {p}")
        return 1
    print(f"ACCEPTED: {directory}: {len(policy_set.files)} files, {len(policy_set.rules)} rules, "
          f"schema-valid, every rule named, no wide-open rule, {len(MATRIX)} authorization cases as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
