# Library sweep

The library sweep goes through every song in your library and looks on Soulseek for a better copy of it. It never
replaces a song by itself. Every better copy comes to you in Telegram next to your file, with three buttons, so
you can listen before you choose. When the copy is the same recording, only better, the pair says so on its first
line. By default it runs once a week and writes to you when it ends.

It is off unless you turn it on, and only the accounts you name get it.

## Turn it on

Set `LIBRARY_SWEEP_USERS` to your Telegram user id and recreate the container. Several ids go comma-separated.
The account must be in `TELEGRAM_ALLOWED_USERS` and save to the library. An account in
`TELEGRAM_CHAT_DELIVERY_USERS`, or a chat switched to chat delivery with `/deliver`, never gets the sweep.

| Variable | Default | What it does |
|----------|---------|--------------|
| `LIBRARY_SWEEP_USERS` | *(empty: off)* | Telegram user ids that get `/sweep`, the reports and the pairs to judge. Empty turns the feature off, MCP tools included |
| `LIBRARY_SWEEP_SCHEDULE` | `weekly:sun:04:00` | When a sweep starts on its own. `weekly:<day>:HH:MM` with a day from `mon` to `sun`, `daily:HH:MM`, or `off` to sweep only with `/sweep` or MCP. The time is the container's local time, so set `TZ`. A start missed while the bot was down runs when it comes back. A bad value stops the bot at start |
| `LIBRARY_SWEEP_PAUSE_SECS` | `20` | Seconds between two songs that searched Soulseek, to keep the load on peers low |
| `LIBRARY_SWEEP_KEEP_DAYS` | `7` | Days a song you replaced with Take new stays in `<library>/.sweep-replaced/` before it is deleted. `0` deletes it at once, `never` never deletes it |

The image ships `fpcalc` from Chromaprint for the same-recording check. A PyPI install without Chromaprint still
sweeps, but judges by length and tags alone, and `/sweep status` says so. Without ffmpeg or the `analysis`
extra the sweep cannot measure anything, so it does not start, and `/sweep status` names what is missing.

## What it checks

The sweep measures every audio file at the top level of the library named `Artist - Title.<ext>`. It does this
once, and again whenever the file changes. It decodes the whole file strictly with
`ffmpeg -xerror -err_detect crccheck+explode`, and reads the spectral cutoff, bit depth and sample rate. That
puts the song in a tier.

| Tier | Meaning | Searched again |
|------|---------|----------------|
| damaged | lossless, but the strict decode found errors | every sweep |
| fake | lossless file whose spectrum stops below 16 kHz, made from an MP3 or AAC | every sweep |
| lossy | MP3, AAC, Ogg and the like | every sweep |
| uncertain | spectrum stops between 16 and 19.5 kHz, a good MP3 in disguise or an old master | every sweep |
| cd | spectrum reaches 19.5 kHz or more | once a month |
| hires | 24-bit above 48 kHz with real content above 24 kHz | never, unless forced |

The worst tiers go first, one song at a time, one download at a time. For each song that is due:

1. The sweep resolves the song from its file name and length, on Spotify first and MusicBrainz second. A song
   with no confident match is left alone.
2. It searches Soulseek and downloads only copies that could beat the song. Those are lossless files the
   spectrum check can read, so FLAC, WAV and AIFF, within 3 seconds of the song's length. A cd song only wants
   24-bit copies above 48 kHz. It tries three copies at most, two for a cd song, and the lossless gate deletes
   fakes on the way.
3. A downloaded copy is better when it decodes cleanly and its spectrum reaches further. A cd song needs genuine
   content above 24 kHz. A fake, uncertain or lossy song needs at least 19.5 kHz and 1 kHz more than its own.
   A damaged song takes any copy that is no worse.
4. The sweep then compares the better copy with the song: is it the same recording, and does it sound as good.

## Same recording or not

Some copies are out whatever they sound like. That covers another artist, a karaoke, tribute or "in the style
of" copy, a live, unplugged, acoustic or remix recording your file is not, and another title. Remaster years
and track numbers don't count as differences.

For the rest, Chromaprint decides. `fpcalc -raw` fingerprints the first 150 seconds of both files, and the sweep
compares the two fingerprints bit by bit. It shifts one against the other by up to 50 seconds, so an intro or a
stretch of silence doesn't hide a match. A similarity of 0.85 or more is the same recording. On a real library
the same recording under another master scored 0.92, another performance of the song 0.60, and unrelated audio
sits near 0.5.

| Fingerprint | Result |
|-------------|--------|
| Same recording, length within 10 s, no new names in its tags | Sent to you as a pair marked "Same recording, better copy" |
| Same recording, but new performers in its tags or more than 10 s off | Sent to you as a pair |
| Another recording, length within 10 s | Sent to you as a pair |
| Another recording, more than 10 s off | Thrown away |

A copy with a fingerprint below 0.6 is another performance of the song (live, re-recorded) rather than
another master of it. You kept your file over 12 of the first 14 such copies. So the sweep sets one aside
and tries the song's other copies first. A closer copy that qualifies is offered instead. When none does,
the one set aside is offered, with "the only copy found" at the end of its reason.

## How it sounds

The cutoff only says how high a spectrum reaches, not how much treble is there. A 24/96 vinyl or tape transfer
can reach 24 kHz and still sound duller than your CD. So before a pair goes to you, the sweep decodes both files
once more and measures three things:

- **Highs**: the energy between 4 and 16 kHz against the mids (1 to 4 kHz), in dB. A copy whose highs are more
  than 3 dB below yours sounds muffled and is thrown away. Volume does not change this measure, and neither
  does a mix with more bass: only highs that were cut. Among the copies you had taken, the muffled ones sat 3.3
  to 7.6 dB below the copy they replaced.
- **Clipping**: flat tops, runs of three or more equal samples above -6 dBFS, counted per minute. A clipped file
  keeps them when it is turned down later. A clean CD shows a few dozen from loud bass, a copy clipped by 4 dB
  thousands. A copy with more than 100 a minute and more than four times yours is thrown away. When your file is
  lossy its count is not compared, because an MP3 decoder never keeps flat tops.
- **Dynamics**: the peak against the average level of the parts that are not silent, in dB. A loudness-war
  remaster is limited until this shrinks, often without a single flat top. A copy more than 3 dB below yours
  is thrown away. Louder is not better here: players level songs anyway, and the quieter master is usually the
  less squashed one.

Both captions of a pair show the measures, for example `Sound: highs -12.3 dB (5.9 dB duller than yours),
clipping none (0/min), dynamics 9.5 dB (4.5 dB less than yours)`, with clipping as none (under 25 a minute),
slight (up to 100) or heavy. Distortion that is part of the source itself, like a worn record, is not caught by
these measures; your ear still decides.

### Songs you took before these checks

Copies you took before 0.22.0 never went through the sound check, and copies taken before 0.24.0 never had
their dynamics measured. At the start of each sweep, every song you took that is not yet compared is measured
against the original it parked, once. One that sounds worse (duller, clipped or squashed) is searched again in
that sweep and every sweep after, first of all and whatever its tier, for a copy that repairs it. Such a copy
only needs a clean decode and a genuine CD cutoff (19.5 kHz), or one at most 0.5 kHz below yours when yours is
lower, and it must sound no worse than your file and no worse than your old copy. Its pair opens with "🔧 Repairs how yours sounds" and says what is wrong
with yours. When no copy on Soulseek does, your old copy itself comes back as a pair, "↩️ Your old copy back",
so your ear chooses between the two. The parked original stays where it is. Any decision on a pair of that
song ends the search for a repair.

When your file has decode errors, a copy counts as better once it decodes cleanly, even with a cutoff up to
0.5 kHz lower than yours. Such a pair opens with "🔧 Repairs the decode errors in yours" and the cutoff
difference, so you can tell a repair from a quality upgrade.

Without `fpcalc` the length decides. Within 1.5 s with no new names or version words is the same recording. Up
to 10 s goes to you, and anything further is thrown away.

## What Take new does

The copy takes your file's place under the same name with its own extension, so `Song.mp3` can become
`Song.flac`. If a different file already holds that name, the sweep leaves both alone. It copies over the tags
your file has and the copy lacks: artist, title, album, date, track number, genre and album artist. It copies
the cover too when the copy has none. The new file gets your file's permissions and owner. It is written under a
temporary name and renamed in one step, so a player never sees half a file. A `.lrc` lyrics file next to the
song keeps working, because the name doesn't change.

Your file is not deleted. It moves to `<library>/.sweep-replaced/<name>.sweep` and is deleted after
`LIBRARY_SWEEP_KEEP_DAYS`. The `.sweep` ending keeps players and transcoders from taking it for a song. To undo
a replacement, move the file back and drop the `.sweep`.

The sweep never deletes any file of yours except the one you chose to replace, and that one only from
`.sweep-replaced` after the wait. Everything else it deletes is a file it downloaded itself.

## In Telegram

Every sweep ends with one message to each account in `LIBRARY_SWEEP_USERS`, whether it was the scheduled one,
a `/sweep`, or one started over MCP. The message says how many songs were checked and gives a count per
outcome: for your ear, copies tried with none qualifying, no better copy, no confident match, already hi-res.
You get it even when nothing changed. If the next scheduled start comes while a sweep is still running, you get a progress line
instead.

Then come the pairs waiting for your ear, up to 10 per message, each as two audio messages.

1. **Yours.** Your file, with its quality and length.
2. **Proposal.** The copy, with its quality and length, why it needs your ear, and the name it would get if you
   keep both. A reason reads like "another recording (fingerprint 0.61), length differs 4.2 s". The buttons sit
   under this one.

    - **Keep mine** deletes the proposal. The sweep won't propose any copy of this song again. The message
      then asks what was wrong with it: Distorted, Muffled, Other version or Other. Answering is optional; the
      answer is stored with the pair, so a later sound check can learn from what you reject.
    - **Take new** replaces your file with the proposal. Your file goes to `.sweep-replaced`.
    - **Keep both** adds the proposal as a song of its own and keeps yours. Its name comes from what sets it
      apart, which is the extra performers in its artist tag, a bracket in its title, or its album. For example
      `Elvis Presley - Always On My Mind (with the Royal Philharmonic Orchestra).flac`.

The decision applies at once and the buttons go away. A pair you leave undecided is not downloaded again. It
comes back with the next sweep's message, or right away with `/sweep reviews`. With a
[local Bot API server](getting-started.md#send-files-over-50-mb-a-local-bot-api-server) files go as they are, up
to 2000 MB. With the cloud API a file over 50 MB arrives as Opus to listen to, and your decision still applies
to the file itself.

The two files reach you at the same loudness. The louder one always sounds better in a quick listen, even when it
is the worse master, so when they differ by 1 dB or more (integrated loudness, EBU R128) the louder one is sent
turned down to the quieter one's level, as a 24-bit FLAC. Its caption says so, for example "🔉 Played 9.6 dB
quieter here, at the other file's loudness; the file is unchanged". Nothing is ever turned up, and the files
themselves stay as they are: Take new puts the proposal in your library at its own level.

| Command | What it does |
|---------|--------------|
| `/sweep` | Start a sweep now |
| `/sweep force` | Start a sweep that checks every song, whatever its tier and last check |
| `/sweep status` | Whether a sweep runs, where it is, the counts so far, songs per tier, pairs waiting, the next scheduled start |
| `/sweep reviews` | Send the pairs waiting for your ear now |

Only accounts that get the sweep see `/sweep` in the command menu.

## Over MCP

With the sweep on, the [MCP server](mcp.md) has four more tools behind the same token as the others.

| Tool | What it does |
|------|--------------|
| `library_sweep_run(force=false)` | Starts a sweep in the background and returns at once with the run id and the status |
| `library_sweep_status()` | The current or last sweep with its position, counts and current file, songs per tier, pairs waiting, the schedule and the next start, and whether `fpcalc` is there |
| `library_sweep_reviews()` | The pairs waiting, each with its stem, why, both files' path, quality, length, cutoff and sound, the fingerprint similarity and the keep-both name |
| `library_sweep_decide(stem, decision, reason)` | `keep_mine`, `take_new` or `keep_both` for the pair of that `Artist - Title`; `reason` (`distorted`, `muffled`, `other_version`, `other`) only with `keep_mine` |

A sweep started over HTTP MCP reports in Telegram like any other. The stdio server can run a sweep too, but it
tells nobody when it ends, so read `library_sweep_status`. Two processes never sweep the same library at once.

## Pacing and restarts

One sweep runs at a time, one song at a time, one download at a time, with `LIBRARY_SWEEP_PAUSE_SECS` between
songs that searched. The strict decode and `fpcalc` run as separate processes at the lowest CPU priority. The
spectrum check runs in a worker thread. None of it runs in the bot's event loop. Every step lands in SQLite at
`DATA_DIR/importer.db`, so a restart continues the sweep where it stopped without checking a song twice. Each
song checked writes one log line:

```text
Sweep 812/2400 Nancy Sinatra - Bang Bang.flac (fake): For your ear: Same recording, better copy: same recording (fingerprint 0.97), length within 0.4 s
```

The library index behind the duplicate check leaves hidden folders out, so `.sweep-replaced` and
`.sweep-review` never show up as duplicates.
