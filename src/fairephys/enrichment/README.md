# fairephys.enrichment

Programmatic metadata enrichment. Operates **post-compilation** on the odML document, filling schema slots from recording files and resolving controlled terms, without modifying the human-curated YAML templates.

## Workflow

```
YAML templates --> compile_to_odml() --> odML doc (human input only)
                                             |
                  per session (curate.py):   |
                    raw file --> neo_extract(file, format)
                                             |
                    neo_enrich_document(doc, neo_data, session_id, subject, task, runs)
                      +- fill empty properties (duration, start time, sampling rate, ...)
                      +- add the Neo Metadata subsection (channels, streams; 'runs' when given)
                                             |
                  openminds_enrich_document(doc) --> (warnings, instances)
                      (finalize_compilation writes <dataset>.instances.yaml
                       and the warnings for --validate)
                                             |
                  save_odml(doc) --> odML on disk (the enriched truth)
                                             |
                             +---------------+---------------+
                             v               v               v
                        build-probes    bids-layout        convert
                        (all read from the enriched odML)
```

The YAML files on disk stay the human-curated input; the odML document on disk is the enriched single source of truth. The sanity checks read the raw files again themselves (`fairephys.formats.open_rawio`).

## Modules

| Module | Source | Fills |
|--------|--------|-------|
| `neo.py` | Recording files (Neo RawIO) | Session timing, channel structure, per-run durations |
| `openminds.py` | openMINDS vocabularies | The instances of controlled values (`.instances.yaml`), warnings for unresolved ones |

## neo.py

`neo_extract(file, format, block_index=0)` walks the parsed RawIO header (signal channels and streams, spike and event channels), the computed values (duration, sampling rate) and the block's `raw_annotations` generically; the formats are the keys of `fairephys.formats.RAW_FORMATS` (`clock: utc` marks a header time that is UTC rather than local; `header: first sample` marks a header time that stamps the first sample rather than Neo's time 0, so a segment's start is the header time plus its t_start relative to the first segment's).

`neo_enrich_document(doc, neo_data, session_id, subject=None, task=None, runs=None)` finds the session entry (by subject and task when session ids repeat, see `fairephys.metadata.sessions`; an unknown session is an error), fills only empty properties (human values win, filled ones get `value_origin` neo) and stores the extraction as the `Neo Metadata` subsection. `runs` lists the run each Neo segment records; each run's duration then goes to `Neo Metadata/runs`. `Hardware/Recording System 'curated streams'` restricts the streams kept.

```bash
python -m fairephys.enrichment.neo recording.ns5 -f blackrock --json
```

## openminds.py

`openminds_enrich_document(doc)` resolves controlled-term values (species, brain regions, licenses, ...) to openMINDS instances with the `openminds_schemas.yaml` registry and returns `(warnings, instances)`; the odML itself is not changed. The instances feed the HTML viewer's links and the openMINDS export; the warnings name values the KG would reject.

## fairephys.events

`fairephys/events.py` reads the events of a recording and of its behaviour file as the datasets need them; what the codes mean and how a lab synchronizes its clocks stay in the datasets.

- `event_times(rawio, channel, block_index=0, seg_index=0)`: times (s) and labels of one event or epoch channel of a parsed Neo RawIO (the Blackrock .nev digital codes, the TDT epocs, the Plexon events), by name or index, through Neo's `get_event_timestamps` and `rescale_event_timestamp`.
- `read_tdt_epocs(tank_dir, store, seg_index=0)`: onsets (s from the start of the block) and float64 values of one TDT epoc store, read from the block's TSQ index in Neo's record layout (`tsq_dtype`, the store's event records of channel 0, as `TdtRawIO`'s event channel returns them; the Tbk header must list the store as an epoc store). `TdtRawIO` itself is not opened: it indexes every channel of every stream of the block for one store (about a minute on a long block against well under a second) and rejects a block whose stream channels hold unequal chunk counts, a layout this reader accepts. `tank_dir` is a tank folder named as its files' prefix (the blocks in start-time order), or a block's .tsq for that block alone.
- `match_codes(a, b)`: the index pairs that align two code sequences (difflib.SequenceMatcher's rule, written out in the docstring); no match is an error.
- `bhv2_trials(path)`: the trials of a MonkeyLogic BHV2 file (number, start, codes and code times, condition, trial error, the whole trial struct), in order; gaps in the numbering, missing fields and values that are not whole numbers are errors.
