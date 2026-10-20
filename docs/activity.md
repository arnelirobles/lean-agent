# Activity

brain keeps a table of what other tools did, so one place shows it across machines and sessions. It is read by the Activity tab in the dashboard and by `brain activity list`. Nothing leaves the machine.

The `activities` table is created the first time a `brain activity` command runs. There is no schema bump, so older brains sharing the same database file keep working.

## Commands

```
brain activity add --source S --action A [--ts T] [--project P] [--target T] [--ok 0|1] [--duration-ms N] [--error E] [--host H]
brain activity list [--source S] [--project P] [--since 24h] [--failed] [--limit N]
brain activity import-baryovm [--file PATH]
```

`list` prints newest first, at most 50 lines by default. `--since` takes a number and one of `s`, `m`, `h`, `d`, `w`. A failed row is marked `FAIL`. `add` checks `--ts` like the importer (UTC, `YYYY-MM-DDTHH:MM:SSZ`), and defaults it to now and `--host` to this machine's name.

Store no secrets. Keep `--error` to the short message, not the command line or its arguments.

## Importing BaryoVM

`import-baryovm` reads `$BARYOVM_HOME/activity.jsonl`, or `~/.baryovm/activity.jsonl`, or `--file`. It starts at the byte offset where the last import stopped, so a second run adds nothing. The offset is kept in `meta` (key `activity_offset:baryovm:<real path>`, symlinks resolved) together with a hash of the file's first line, and written in the same transaction as the rows, so two imports at once cannot add a line twice. The file is read from the start when it is smaller than the offset, when its first line changed, or when the byte before the offset is not a newline. That covers a rotated file that has grown past the old offset. A last line with no newline yet is left for the next import.

A line that is not JSON or not valid UTF-8, lacks `ts`, `action` or `ok`, has the wrong types, or has a `duration_ms` outside 0 to 2^63-1 is skipped and counted. A `--file` that cannot be read prints a message and exits 1. A line with a field outside the list below is skipped too, and its content is not stored. The command prints `imported N, skipped M`.

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

`raw` keeps the source line as it was, without its line ending.
