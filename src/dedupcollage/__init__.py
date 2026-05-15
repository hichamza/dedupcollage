"""DedupCollage — photo & video recovery deduplication.

Clusters near-duplicate copies of the same image, picks the least-corrupt one
as the winner, and grafts metadata from sibling copies that have intact EXIF
where the winner doesn't.
"""

__version__ = "0.1.0"
__app_name__ = "DedupCollage"
