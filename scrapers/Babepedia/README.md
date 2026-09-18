# Babepedia (scraper)

Performer scraper for babepedia.com — scrape by URL or by name.

Originally from [CommunityScrapers](https://github.com/stashapp/CommunityScrapers) and
kept here after it stopped receiving updates from that source.

## Requirements

Needs `py_common`, CommunityScrapers' shared Python helper library, installed as its
own scraper/plugin in Stash. It is not bundled here — if Stash has never had a
CommunityScrapers source configured, install `py_common` from
https://stashapp.github.io/CommunityScrapers/stable/index.yml first, or this scraper
fails on import.

Also needs the `lxml` and `requests` Python packages available to Stash's Python.
