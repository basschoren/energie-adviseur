"""Zorgt dat Home Assistant onze custom_components vindt.

De testkern zet zijn eigen configuratiemap vooraan in sys.path; door het pakket
hier al te importeren wint de map uit dit project.
"""

import custom_components.energie_adviseur  # noqa: F401
