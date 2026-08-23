# ScrapeAll Settings

Configuration for the [ScrapeAll](../../scrapers/ScrapeAll/) scraper.

A scraper has nowhere to keep settings a user can edit — Stash gives that only to
plugins, and stores the values in its own config where a scraper can read them
back (`configuration.plugins`). So this plugin does no scraping: it exists for its
`settings` block, plus one task that reports what those settings mean.

Settings → Plugins → **ScrapeAll Settings**.

## Settings

### Allowed fields

Comma-separated whitelist of the scene fields ScrapeAll may fill. **Empty means
all of them.** Anything listed restricts it to exactly that list.

```
title, code, date, director, details, urls, studio, performers, tags, groups
```

Aliases are accepted (`url` → `urls`, `performer` → `performers`, `tag` → `tags`,
`movie`/`movies` → `groups`, `description` → `details`) and logged as warnings so
you can see how a value was read.

An unknown name is an error in the log and is dropped. If **nothing** in the list
is valid, ScrapeAll returns nothing at all rather than falling back to writing
everything — a typo should not turn a restriction into free rein.

### Ignored scrapers

Comma-separated sources to skip, matched case-insensitively against a scraper's
name or id and against a stash-box's name or endpoint. Each skip is logged:

```
SKIP  NameSite - ignored by the ScrapeAllSettings plugin
```

Semicolons and newlines work as separators too, in both fields.

## Task: Show effective settings

Settings → Tasks → Plugin Tasks → **Show effective settings**. Parses the values
above exactly as the scraper will and logs the result, so a typo surfaces on
demand instead of silently dropping a field:

```
ScrapeAllSettings - effective configuration for the ScrapeAll scraper
  raw values     : {"allowedFields": "urls, details, performers", "ignoredScrapers": "NameSite"}
  allowed fields : details, performers, urls
  ignored sources: namesite

How each allowed field is combined across sources:
  x title      first source that returned one
    details    one line per source that identified the scene
    urls       the scene's existing URLs plus every new one, duplicates dropped
    performers union of every source, de-duplicated
  ("x" marks a field the whitelist excludes)
```

The task is read-only. It changes no setting and touches no scene.

## Requirements

Python 3.8+, standard library only.

## Notes

The setting names, the field vocabulary and the per-field merge rules all live in
`stash_common/settings.py`, which both this plugin and the scraper import, so the
two cannot disagree about what a value means. The hint under *Allowed fields* in
the yml was copied from `settings.hint()`; it is plain text, so adding a field to
`MERGE_RULES` means updating that line by hand.

Changing a setting takes effect on the next scrape; nothing is cached.
