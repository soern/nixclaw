# Print Skill

> **Example skill, disabled by default.** Enable it by uncommenting the `print` entry in
> `config/skills.yaml`. It is kept as a complete working example of a skill whose script
> asks the users for approval via a Signal poll before acting (`ipp_print.py print`); see
> `skills/skill-creator/SKILL.md` "Rare case: skills with critical actions". It was tested
> with one Brother inkjet; other printers may need changes.

Print documents and images on an IPP/AirPrint network printer — **without CUPS**, with
full support for **multi-page PDFs** and **double-sided (duplex) printing**.

Use it when the user wants to print a file (PDF, image, text) or change printer settings.

## The one script

Everything goes through `/app/skills/print/scripts/ipp_print.py`. It converts the file,
picks the document format the printer accepts and submits **one** IPP job.

```
python3 /app/skills/print/scripts/ipp_print.py probe    <ipp_uri>
python3 /app/skills/print/scripts/ipp_print.py validate <ipp_uri> <file> [options]   # dry run, no paper
python3 /app/skills/print/scripts/ipp_print.py print    <ipp_uri> <file> [options]   # asks the users first
python3 /app/skills/print/scripts/ipp_print.py jobs     <ipp_uri>
python3 /app/skills/print/scripts/ipp_print.py cancel   <ipp_uri> <job-id|all>
```

**Options for `print` / `validate`:**

| Option | Values | Default |
|---|---|---|
| `--duplex` | shorthand for `--sides=two-sided-long-edge` | off |
| `--sides` | `one-sided`, `two-sided-long-edge`, `two-sided-short-edge` | `one-sided` |
| `--color` | `color`, `monochrome`, `auto`, `auto-monochrome` (`mono`/`bw`/`sw` are aliases) | `monochrome` |
| `--quality` | `draft`, `normal`, `high` | `normal` |
| `--copies` | integer; ALL copies in one job (max. 50 sheets per command) | `1` |
| `--media` | e.g. `iso_a4_210x297mm` | `iso_a4_210x297mm` |
| `--format` | `auto`, `raster`, `jpeg` | `auto` |
| `--wait` | poll the job until it is finished (slow, can hit the bash timeout) | off |

`--duplex` for text documents and `--quality=draft --color=monochrome` for anything
that is not a photo — that saves a lot of ink and paper.

## How to Use

### Step 1: Check configuration

Printer settings live in the user's context dir: `config/print.yaml`. Read it with
`file_read`; if missing/incomplete, ask the user **in one message** for printer name,
IPP URI and defaults (colour, duplex, quality), then write it with `file_write`.

```yaml
printer:
  name: "Office"
  ipp_uri: "ipp://printer.local:631/ipp/print"

defaults:
  sides: "two-sided-long-edge"   # or one-sided
  color_mode: "monochrome"       # color | monochrome | auto
  quality: "normal"              # draft | normal | high
  media: "iso_a4_210x297mm"
  n_up: 1
```

### Step 2: Probe the printer (first time, or whenever something fails)

`probe` shows the supported formats, sides, colour modes, the printer state, the queue
and low ink. Never guess a printer's capabilities — ask it.

### Step 3: Optional preview

For multi-page or unclear documents you may send a preview image first:
`pdftoppm -r 60 -png -f 1 -l 4 <file>.pdf /app/tmp/prev`, combine with
`montage /app/tmp/prev-*.png -tile 4x1 -geometry +4+4 /app/tmp/preview.png`, copy it into
the channel's `attachments/` folder and send it with `send_signal_attachment`.

### Step 4: Print — the users confirm via a poll

Run `print` as **one standalone bash command**, with a larger bash timeout
(e.g. `timeout: 180`) — converting and uploading can take longer than the default 30 s:

```bash
python3 /app/skills/print/scripts/ipp_print.py print "<ipp_uri>" <file> --duplex --copies=2
```

The script checks and converts everything but prints nothing. The harness then posts
a summary (sheets, ink warnings) and a **poll** to the chat and your turn pauses until
someone answers. The tool result tells you the outcome:

- approved → the output of the real print run (`Sent: job <id> …`). Done.
- `NOT RUN …` → nothing was printed; follow the text of the result.
- `Error: …` before any poll → fix the cause (wrong option, unsupported setting,
  more than 50 sheets) and call `print` again.

**Rules:**

- The poll IS the confirmation. Do not ask "Shall I print?" in text first.
- One `print` call per document. For several copies use `--copies=N` — never a loop
  and never one job per copy or page.
- Never pipe, redirect, background or `nohup` a print command and never chain it
  (`|`, `>`, `&`, `;`, `&&`): that breaks the approval. Only `2>&1`/`|&` is detected
  and refused (exit 3) — the rest is NOT detected, so it is on you.
- After any error or timeout run `jobs` **before anything else**. Never resubmit a
  job blindly — it may already be in the queue.
- Progress of a submitted job: `jobs`. `print` does not wait for completion.

## Use cases

| Goal | Command |
|---|---|
| Multi-page PDF, double-sided, B/W | `print <uri> doc.pdf --duplex --color=monochrome` |
| Multi-page PDF, single-sided draft | `print <uri> doc.pdf --quality=draft --color=monochrome` |
| Photo, colour, best quality | `print <uri> photo.jpg --color=color --quality=high` |
| 2 copies, double-sided | `print <uri> doc.pdf --duplex --copies=2` |
| Check before wasting paper | `validate <uri> doc.pdf --duplex` |
| Clear a stuck queue | `cancel <uri> all` |

### N-up (multiple pages per sheet)
Do the N-up layout **in the PDF**, then print that PDF normally — never stitch page
images together by hand:
```bash
pdfjam --nup 2x1 --landscape doc.pdf -o /app/tmp/doc_2up.pdf   # if pdfjam available
python3 /app/skills/print/scripts/ipp_print.py print "<uri>" /app/tmp/doc_2up.pdf --duplex
```
Fallback without pdfjam: render pages with `pdftoppm -r 150 -png`, `montage` them into
one A4-shaped image per sheet, convert back to a **PDF** (`convert ... out.pdf`), print that.

## How it works (why this is not the old JPEG hack)

Most AirPrint printers do **not** accept `application/pdf`. They do accept
**`image/pwg-raster`**, which is a *multi-page* format. The script therefore:

1. Fetches a PPD straight from the printer's own IPP attributes via `driverless`
   (cached under `/app/tmp/ipp-ppd-cache/`) — no CUPS daemon, no printer queue.
2. Converts the PDF to multi-page PWG Raster with
   `/usr/lib/cups/filter/pdftoraster` (`FINAL_CONTENT_TYPE=image/pwg-raster`),
   setting `Duplex=DuplexNoTumble`/`DuplexTumble` in the raster header.
3. Submits **one** IPP `Print-Job` containing all pages, with `sides`,
   `print-color-mode`, `print-quality` and `media` as job attributes and
   `ipp-attribute-fidelity=true`. `copies` is never sent: copies are made by
   repeating the pages inside that raster job (with a blank back page per copy for
   odd duplex counts); a JPEG with `--copies` > 1 takes the raster path.

Images are first centred onto an A4 page, then take the raster path or, for a plain
single photo, the direct JPEG path.

## Troubleshooting

Run `probe` first — it answers most questions immediately.

| Symptom | Cause | Fix |
|---|---|---|
| `client-error-document-format-not-supported` (0x040A) | You sent a format the printer rejects (usually PDF). | Use `--format=raster` (default `auto` does this). Check `document-format-supported` in `probe`. |
| `client-error-attributes-or-values-not-supported` (0x040B) | The job is sent with `ipp-attribute-fidelity=true`, so the printer **refuses** settings it cannot honour instead of printing with defaults. Nothing was printed; the script lists the attributes. | Use a value from the matching `*-supported` list in `probe`, then `print` again (new poll). |
| `successful-ok-ignored-or-substituted-attributes` | The printer ignored the fidelity request and changed settings. The script cancels that job at once and exits 1, but some pages may already have printed. | Tell the users what happened; do NOT print again without asking them. Run `jobs` to confirm the job is gone (always if no job-id came back); use values from `probe`. |
| Asked for N copies, got 1 | Many printers ignore `copies` for raster jobs (the Brother does). | Cannot happen any more: the script repeats the pages inside the ONE job and never sends `copies`. Never loop. |
| Job reported `completed`, but everything printed single-sided | `sides` was never sent, or the pages were sent as separate jobs. | Duplex needs ONE job with ALL pages. Always `--duplex`, never a loop over pages. |
| Job sits at `pending` forever, nothing prints | Stale jobs in the queue, or paper/jam problem. | `jobs` to inspect, `cancel all` to clear, then check the physical device. |
| Back side upside-down | Wrong duplex edge for the layout. | Portrait text → `--sides=two-sided-long-edge`. Landscape/flip-up → `--sides=two-sided-short-edge`. |
| Output faint or streaky | Low ink/toner — `probe` warns about this. | Tell the user which cartridge is low. It is not a software problem. |
| "Printer crashed, beeping and flashing" | Something sent the IPP `Identify-Printer` operation. | That is a *deliberate* locate-me beep/flash, harmless. This script never sends it. |

### Example: capabilities of a Brother MFC-J4540DW

Measured via `probe` on the printer this skill was developed with (yours will differ):

```
document-format-supported  = application/octet-stream, image/urf, image/jpeg,
                             image/pwg-raster, application/vnd.brother-hbp
pdf-versions-supported     = none          → PDF is NOT accepted, must rasterise
sides-supported            = one-sided, two-sided-long-edge, two-sided-short-edge
print-color-mode-supported = auto, color, monochrome, auto-monochrome
print-quality-supported    = 3 (draft), 4 (normal), 5 (high)
printer-resolution         = 600dpi only
```

Confirmed by real print jobs: a 2-page PDF printed as 2 sheets one-sided, and a
4-page PDF printed as **2 sheets double-sided** in a single job.

### Historical bugs — do not reintroduce

These were real, each cost hours of debugging:

1. **`print-quality` enum is 3=draft, 4=normal, 5=high.** An older script used
   `{draft:3, normal:2, high:1}`; 1 and 2 are invalid and were silently discarded.
2. **`print-color-mode` value is `monochrome`, not `mono`.** `mono` is rejected.
3. **Never let an IPP library guess attribute value tags.** `pyipp` has no tag mapping
   for `print-color-mode`, so it silently serialised it to *nothing* — the attribute
   never reached the printer and every "B/W" job printed in colour. The current script
   sets every value tag explicitly and refuses unknown attributes.
4. **Always read the `unsupported-attributes` group** of the response. A job can be
   accepted and still ignore all your settings.
5. **The IPP status ranges are 0x04xx = client errors, 0x05xx = server errors.** An
   older table had them swapped, which turned "document format not supported" into a
   phantom "clogged queue" diagnosis and produced a whole series of wrong workarounds
   ("only JPEG works", "one job per page", "wait 8 s between jobs"). All of that is
   obsolete — see the capability table above.
6. **The Brother ignores `copies` for PWG raster** (`successful-ok-ignored-or-substituted-attributes`,
   unsupported `copies`). Do not "fix" that with one job per copy — that is how 9 copies
   became 21 jobs (2026-10-06), and do not send `copies` at all — with fidelity on, the
   printer would reject the job. Copies are made client-side inside the one job.

## Notes

- Requires the `bash` tool → PRIVILEGED users only. Use `file_list`/`file_search` to
  locate files in `attachments/`.
- `print` only ever submits after the users approved the poll; `validate`, `probe`,
  `jobs` and `cancel` need no approval.
- Requirements in the container: `poppler-utils`, `cups-filters` (for
  `/usr/lib/cups/filter/pdftoraster` and `driverless`), ImageMagick. No `pyipp`,
  no CUPS daemon, no `lp`/`lpr`.
- Temporary files go to `/app/tmp/` only.
