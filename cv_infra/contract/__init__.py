"""The verify contract: what a request must be before a single GPU second is spent.

Deliberately re-exports NOTHING — every consumer imports the submodule it needs
(``inputs`` / ``cases`` / ``verdict`` / ``pict`` / ``errors``). A package ``__init__``
that imports its siblings decides the loaded module set for everyone, which is how the
"stdlib-only contract layer" property gets lost without anyone editing the layer.
"""
