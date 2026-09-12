# My Performer Body Calculator

Tags performers from their measurements, height and weight — body shape, breast and butt
size, cup, height type and BMI.

A fork of **[Performer Body Calculator](https://github.com/stg-annon/StashScripts/tree/main/plugins/performerBodyCalculator)**
by [@stg-annon](https://github.com/stg-annon) (GPL-3.0, see `LICENSE`). Every category,
threshold and tag description is his. Two things are different, and they are the reason
the fork exists.

## 1. A measurements triple is a bust, not a bra band

The original read every string that had a cup letter in it as a **bra size**, and derived
the bust from it:

```
bust = band + cup difference
```

That is the western bra-size convention, and it is right for a bra size. It is wrong for
a performer's `measurements` field, which is a bust-waist-hips triple:

```
90G-60-80     ->  90/2.54 = 35.4  ->  35.4 + 7 (G)  = 42.4" bust
48DDDD-24-37  ->                      48   + 7 (G)  = 55"   bust
40J-24-37     ->                      40   + 10 (J) = 50"   bust
```

Busts ten to eighteen inches larger than the hips, from strings that already stated the
bust. That is where every `could not classify bodyshape bust=42 waist=24 hips=31` came
from — not a hole in the classifier, a wrong bust handed to it.

`48DDDD-24-37` is what settles the question: a 48-inch bra **band** under a 24-inch waist
is not a body. The 48 is a bust.

Now, in both unit systems:

```
90G-60-80     ->  35.4" bust, 23.6" waist, 31.5" hips, cup G     ->  Top Hourglass
48DDDD-24-37  ->  48"   bust, 24"   waist, 37"   hips, cup DDDD
40J-24-37     ->  40"   bust, 24"   waist, 37"   hips, cup J     ->  Top Hourglass
```

### How the leading number's meaning is decided

By the **shape of the measurement set**, never by its units, and the answer is recorded
on the result as `measurement_type` and `first_value_is_bust` so nothing downstream can
re-derive it differently.

| Input | Leading number | Type |
| --- | --- | --- |
| `90G-60-80`, `48DDDD-24-37`, `32D-28-34` | **bust circumference** | `*_bust` |
| `36-28-34`, `86/64/89` | **bust circumference** | `*_bust` |
| `32D`, `75E` (no waist and hips) | **bra band** | `*_band` |

A triple is a bust-waist-hips measurement. A number written with a cup letter and nothing
else is a bra size, which is a band by definition — and that lone form is the only place
`bust = band + cup difference` survives.

Units decide the *conversion* only: values above 50 are centimetres, the same rule the
original used, applied to whichever of bust, waist and hips are present.

> **What this gives up.** In western performer databases `32D-28-34` really is a bra size,
> and its true bust is nearer 36 than 32. A triple cannot be read both ways at once, and
> a first number that is plainly a bust — 44, 48, and a 24-inch waist beside it — is the
> data that has to work. Such a record now reads a cup size low. If you have both kinds
> of data, the discriminator would be the waist: a band sits within a few inches of it,
> a bust does not.

A triple with a cup gets an **estimated band** (`bust − cup difference`), because breast
size and the BMI breast-weight correction both need one and a triple does not state it.
It is flagged as estimated and never reported as measured.

### Accepted spellings

Cup in brackets or not, before or after the number, any case, spaces anywhere:

```
88(E)-58-89   88E-58-89   88 (E) - 58 - 89   88e-58-89   E88-58-89
```

all parse to the same measurement. Every format the original read is still read:
`32D-28-34`, `D32-28-34`, `36-28-34`, `86/64/89`, `32D`, `32D (81D)`, `DDD38-None-None`,
`NoneNone-23-35` — the triples among them now yielding a bust rather than a band.

## 2. Two tasks instead of one

| Task | What it does |
| --- | --- |
| **Add New Performer Body Calculations** | Calculates only performers this plugin has not calculated before. Removes nothing. |
| **Full Update Performer Body Calculations** | The original's behaviour: strip this plugin's tags off every performer, clear the markers, recalculate everything. |
| **Destroy Managed Tags** | Deletes this plugin's tags from Stash entirely. |

Both calculation tasks run the same pipeline, so neither can drift from the other.

### The processed marker

Add New knows what it has seen from a **performer custom field**:

```
My Performer Body Calculator = v1
```

A version rather than a boolean, so that changing how a body is calculated can make every
performer eligible again without a full update: bump `CALCULATOR_VERSION` and last
season's marker stops matching.

Stash's performer custom fields are a free-form map (`Performer.custom_fields: Map!`) —
there is no schema to register a field in and nothing to create before writing one. The
first write brings the field into existence. `CustomFieldsInput.partial` is used, so
writing the marker cannot disturb any other custom field you keep on your performers.

### When a marker is written

Only after a performer has been processed, and only for a performer that was successfully
*read*:

* **blank measurements**, **unreadable measurements**, or **a shape no category covers**
  → marked. These are answers about the data, they will not change on a retry, and
  leaving them unmarked would mean re-reading those performers on every Add New for as
  long as the library exists.
* **an exception, a failed tag write, a GraphQL error** → not marked. The performer comes
  round again next time. Adding the same tag twice is harmless, so a retry cannot do
  damage.

### What Full Update removes

The tag ids collected while finding the managed tags — the original's `all_tag_ids`,
through the original's `enumtag_stash_init` — in one bulk call:

```python
"tag_ids": {"ids": all_tag_ids, "mode": "REMOVE"}
```

So a tag you added by hand, a tag from another plugin, and a tag the original Performer
Body Calculator manages are all invisible to it. It is **not** `destroy_managed_tags`:
the tags themselves stay, with their aliases, descriptions and any renaming you have done
to them. Only the assignments go.

## Tags

Found by alias, so you can rename a tag or merge it into one you already had and the
plugin still finds it next run. This plugin's alias prefix is **`MPBC:`** and its tags
carry `[Managed By: MPBC Plugin]` in their description — both different from the
original's `PBC:`, which is what keeps the two plugins' removal passes out of each
other's way.

Configure which tag groups to use by editing `config.py` (copied from
`example_config.py` on first run) and commenting out the ones you do not want.

## Body shape classification

The rules are unchanged apart from one comparison. They come from
[FFIT for Apparel](https://en.wikipedia.org/wiki/Female_body_shape#FFIT_for_Apparel_measurements),
and FFIT itself leaves two regions of the proportion space unassigned:

| | Uncovered |
| --- | --- |
| A | `bust-hips >= 10` and `bust-waist >= 9` — a bust far larger than the hips with a defined waist: past the top hourglass ceiling, too defined for an inverted triangle |
| B | `hips-bust >= 10` and `hips-waist >= 9` and `hips/waist <= 1.193` — the same in reverse, on a frame wide enough to keep the ratio low |

They are reported, not patched over. Widening a category to swallow them would put a name
on a shape no reference gives one to, and a wrong tag is worse than an absent one. The
warning says so:

```
could not classify bodyshape:
bust=45.0 waist=25.0 hips=32.0
bust-waist=20.0
hips-waist=7.0
bust-hips=13.0
hips/waist=1.280
no category covers bust-hips >= 10 and bust-waist >= 9 - a bust far larger than the
hips, with a defined waist - past the top hourglass ceiling, but too defined for an
inverted triangle
```

**The one changed rule**: spoon's hip/waist ratio test is `>= 1.193`, as FFIT gives it.
The original transcribed it as a strict `>`, which left the ratio 1.193 itself in no
category at all.

Two crashes are also fixed, both of which ended a performer's processing before any tag
was assigned: a null `measurements` and a null `ethnicity`, which is what Stash returns
for a performer nobody has filled in.

## Requirements

* `stashapp-tools` — `pip install stashapp-tools`
* Stash v0.28 or newer, for performer custom fields. An older one is reported plainly
  rather than as a raw GraphQL error.

## Running it alongside the original

You can, but there is one thing to know: Stash tag names are unique, and both plugins use
the same names (`BodyShape.HOURGLASS` and so on). So on a library that has run the
original, this plugin finds those tags by name and **shares** them — it adds its own
`MPBC:` alias alongside the original's `PBC:` one rather than making a second set. Each
plugin's Full Update then recalculates the shared tags according to its own reading of
the measurements. If you want the corrected metric handling, run this one and leave the
original's tasks alone.

## Credits

Original plugin and all of the body calculations: [@stg-annon](https://github.com/stg-annon),
with @badde57, @feederbox826 and @melon-scientist. Licensed GPL-3.0; this fork keeps that
licence.
