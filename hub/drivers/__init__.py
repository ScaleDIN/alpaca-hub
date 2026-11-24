"""Built-in drivers: none any more. Every driver is a folder of its own (drivers/<key>/ here, installed ones under
%ProgramData%\\AlpacaHub\\drivers), run in its own process (REFACTOR.md steps 7-10). hub/catalog.py still looks here
for modules that export DRIVERS (as "built in"), which is how drivers were added before.

all_drivers() loads the source-folder drivers into this process, for --in-process and the in-process tests only.
"""


def all_drivers():
    """One instance of every driver class in the source folders (and any built-in module), imported here."""
    from ..catalog import Catalog, load_in_process
    return load_in_process(Catalog(installed="", builtin=True))
