"""Synthetic drawing generator for the fictional flagship store.

Modules: ``brands`` (fictional brand catalog), ``layout`` (R0 truth model), ``revisions`` (R1/R2/H1 edits
and ground-truth change sets), ``dxf_writer`` (clean/messy DXF output) and ``drawings`` (orchestration
and CLI: ``python -m cad2anything.mockgen.drawings``).
"""

from cad2anything.mockgen.brands import Brand, brands_to_rows, generate_brands

__all__ = ["Brand", "brands_to_rows", "generate_brands"]
