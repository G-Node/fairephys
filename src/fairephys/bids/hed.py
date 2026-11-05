"""HED (Hierarchical Event Descriptors) vocabulary shared by the events.json sidecars.

The schema version (dataset_description.json HEDVersion) and the tags of the
event types every task has: trial and session boundaries, reward. A dataset
composes its event_type -> HED mapping from these and its own task's tags,
next to its events parsing.
"""

from __future__ import annotations

HED_VERSION = "8.4.0"

TRIAL_START = "Experimental-trial, Temporal-marker, Label/trial-start"
TRIAL_END = "Experimental-trial, Temporal-marker, Label/trial-end"

SESSION_START = "Recording, Temporal-marker, Label/session-start"
SESSION_END = "Recording, Temporal-marker, Label/session-end"

REWARD = "Sensory-event, Reward"
