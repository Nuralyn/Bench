"""Mark the test suite as a regular package.

A regular package (rather than an implicit namespace package) keeps
``python -m unittest tests.test_x`` and ``from tests._ledger_fixtures
import ...`` resolving to this directory even when an unrelated
``tests`` package exists elsewhere on sys.path, because a regular
package anywhere on the path outranks every namespace portion.
"""
