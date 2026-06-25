"""Allow running as: python -m local_goedel.cli"""
from local_goedel.cli import entrypoint
import sys
sys.exit(entrypoint())
