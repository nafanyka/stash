from body_tags import *

# Log level will skip logging lower levels
log_level = 'INFO'

# Comment out tags you dont want in Stash
TAGS_TO_USE = (
    # PBCError,
    BodyShape,
    BodyType,
    BreastSize,
    ButtSize,
    BreastCup,
    HeightType,
    HipSize,
    BodyMassIndex
)
# Recalculate a single performer when Stash says they changed.
#
# The plugin subscribes to Performer.Create.Post and Performer.Update.Post. An update is
# only acted on when it carried one of the fields the calculation reads - measurements,
# height, weight, ethnicity, gender - so the plugin does not answer its own tag writes,
# and only the difference in tags is written.
#
# Set to False to leave tagging entirely to the two tasks.
RECALCULATE_ON_UPDATE = True
