# Activity

brain keeps a table of what other tools did, so one place shows it across machines and sessions. It is read by the Activity tab in the dashboard and by `brain activity list`. Nothing leaves the machine.

## Commands

```
brain activity add --source S --action A [--ts T] [--project P] [--target T] [--ok 0|1] [--duration-ms N] [--error E] [--host H]
brain activity list [--source S] [--project P] [--since 24h] [--failed] [--limit N]
brain activity import-baryovm [--file PATH]
```

`list` prints newest first, at most 50 lines by default. `--since` takes a number and one of `s`, `m`, `h`, `d`, `w`. A failed row is marked `FAIL`. `add` defaults `--ts` to now and `--host` to this machine's name.

Store no secrets. Keep `--error` to the short message, not the command line or its arguments.

## Importing BaryoVM

`import-baryovm` reads `$BARYOVM_HOME/activity.jsonl`, or `~/.baryovm/activity.jsonl`, or `--file`. It starts at the byte offset where the last import stopped, so a second run adds nothing. The offset is kept in `meta` (key `activity_offset:baryovm:<path>`) and written in the same transaction as the rows, so two imports at once cannot add a line twice. A file smaller than the stored offset was rotated and is read from the start. A last line with no newline yet is left for the next import.

A line that is not JSON, lacks `ts`, `action` or `ok`, or has the wrong types is skipped and counted. A line with a field outside the list below is skipped too, and its content is not stored. The command prints `imported N, skipped M`.

## BaryoVM line format

One JSON object per line, written by BaryoVM (see BaryoDev/BaryoVM#126):

| field | type | stored as |
|---|---|---|
| `ts` | UTC RFC 3339 string, required | `ts` (whole seconds) |
| `version` | string | only in `raw` |
| `action` | string, required, the command path such as `stack release` | `action` |
| `vm` | string | `target` |
| `stack` | string | `project` |
| `ok` | boolean, required | `ok` |
| `duration_ms` | integer | `duration_ms` |
| `error` | string, on failure | `error` |
| `host` | string, the operator's machine | `host` |

`raw` keeps the source line as it was.
