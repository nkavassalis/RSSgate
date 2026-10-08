# Example feed: newly trending Hugging Face models

A public feed of models that have just started trending on Hugging Face,
one entry per model, linking to its model page:

    https://du62m9e5v3var.cloudfront.net/feed.xml

It's hosted for anyone to use. Paired with the settings below, RSSgate turns
each new entry into a one-line description of what the model actually does,
written from its model card.

## Add it

With RSSgate running:

```sh
./add-feed.sh                                   # http://127.0.0.1:8088
RSSGATE_URL=https://<machine>.<tailnet>.ts.net ./add-feed.sh
```

Or add the URL in the admin panel and copy the settings from `feed.json`
into the feed's cog panel.

## Why these settings

| setting | value | why |
|---|---|---|
| custom prompt | `Summarize the models capabilities succinctly.` | a model card isn't news; ask for what the model does, not what happened |
| digest length | `terse` (one sentence, 20 words at most) | it's a list to scan, one line per model |
| max input chars | `6000` | the top of a model card carries the description; benchmark tables and usage code further down add tokens, not meaning |
| prune entries that vanish | on | it's a trending list, so models drop off it; this keeps the feed to what is trending now |
| category | `ai` | for the sidebar filter |

The digest length only sets how long the digest is. What goes in it comes
from the prompt, so you can swap in your own (licences, parameter counts,
"good for running locally?") and keep `terse`.
