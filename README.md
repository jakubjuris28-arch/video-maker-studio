# Video Maker Studio

ONE app for all your faceless channels. Pick the **author** from the dropdown, type a
title, click Generate — you get the script + the locked dark-gold mindmap, exactly like
the individual apps did, because each author carries their own verbatim locked format
under the hood.

## Run

```bash
cd /Users/jakubjuris/Downloads/video-maker-studio
python3 server.py
```
Open **http://127.0.0.1:5200**

## Authors built in

| Author | Voice | Premise | Affirmation anchor |
|---|---|---|---|
| Florence Scovel Shinn | warm spiritual mentor | Bible verse | "my word is my wand" |
| Neville Goddard | mystical mentor | Bible verse (his reading) | "imagination creates my reality" |
| Joseph Murphy | scientific preacher | Bible verse (law of mind) | "my subconscious brings it to pass" |
| Louise Hay | nurturing mother | principle of Life | "all is well in my world" |
| Napoleon Hill | salesman-professor | Hill principle | "what I conceive and believe, I achieve" |
| Emmet Fox | Irish schoolmaster-preacher | Bible verse (spiritual law) | "I think of God instead" |
| Carl Jung | thoughtful essayist | verified Jung insight | "I make the unconscious conscious" |

Each author also carries their own: image motifs, mindmap signature (central node),
product/funnel defaults, CTA default, safety rails (health, accuracy, fake quotes),
and Christian-safe / psychology-safe vocabulary rules where relevant.

## Per-author settings (optional, in `.env`)

Global: `PRODUCT_NAME=` / `FUNNEL_CHANNEL=` apply to all authors.
Per author (wins over global): `PRODUCT_NAME_NEVILLE=`, `FUNNEL_CHANNEL_HILL=`, etc.
(keys: SHINN, NEVILLE, MURPHY, HAY, HILL, FOX, JUNG)

## Research

All the niche research lives in `research/<author>/` — the audience brief and the
titles & thumbnails bank per author. Read those before packaging videos.

## Files & behavior

Identical to the previous apps: background jobs + progress polling, the V39-locked
`.xmind` (4-sentence key bodies, quoted affirmations, The Daily Covenant, closing with
the final affirmation), kie.ai explanatory images, auto-expand length, results page
shows script + mindmap (PNGs/prompts still saved to `output/<job_id>/`).
