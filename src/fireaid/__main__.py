"""
Enables ``python -m fireaid [module] [arg] ...``, like ``python -m fire``.
"""

import sys

import fireaid

try:
    from fire.__main__ import import_module
except ImportError:
    import_module = None


def main(args):
    if import_module is None:
        sys.exit("python -m fireaid requires a Fire version with python -m fire")

    if len(args) < 2:
        print("usage: python -m fireaid [module] [arg] ...")
        sys.exit(1)

    module, module_name = import_module(args[1])
    fireaid.Fire(module, name=module_name, command=args[2:])


if __name__ == "__main__":
    main(sys.argv)
