"""The browse brain (docs/design/browse-brain.md): the owner's browser history, read on this
machine only, filtered (`privacy`) and turned into Keepfold pages (`build`). `sources` finds and
reads the browsers' own SQLite files; `lists` holds the built-in domain and word lists.

The filter: `filter_record(url, title, settings=load_settings()) -> Kept | None`.
"""

from keepfold.browse.privacy import Kept, Settings, filter_record, load_settings

__all__ = ["Kept", "Settings", "filter_record", "load_settings"]
