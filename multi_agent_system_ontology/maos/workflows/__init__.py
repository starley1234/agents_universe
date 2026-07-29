"""Прикладные workflow MAOS: сайт и опись помещения."""
from .room_inventory import validate_inventory
from .website_build import website_steps
__all__ = ["validate_inventory", "website_steps"]
