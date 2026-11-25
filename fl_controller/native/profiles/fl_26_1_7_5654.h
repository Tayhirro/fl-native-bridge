#ifndef FL_26_1_7_5654_H
#define FL_26_1_7_5654_H

/* Windows x64 engine profile for FL Studio 26.1.7.5654. */
static const BYTE engine_sha256[32] = {
    0x6d, 0x9c, 0x5a, 0xb0, 0x9c, 0x1d, 0x55, 0x94,
    0x01, 0x2a, 0xc2, 0xc3, 0x1c, 0xf9, 0xed, 0x08,
    0xe7, 0x3f, 0x42, 0x16, 0x4e, 0xd0, 0x96, 0x4c,
    0xcc, 0xae, 0x10, 0xa9, 0x5d, 0x9b, 0x8e, 0x02,
};

enum {
    TEXT_START = 0x1000,
    TEXT_END = 0xfa44f0,
    MIXER_ARRAY = 0x11e06a0,
    MAIN_THREAD = 0x11e4f18,
    TRACK_STRIDE = 0x1478,
    SLOT_ARRAY = 0x134c,
    LOAD_EFFECT = 0xf0,
    TRACK_VALID = 0xea71b0,
    SLOT_OCCUPIED = 0xf2f7c0,
    FROM_UTF8 = 0x1c6e0,
    FREE_STRING = 0x12d60,
    PROJECT_MANAGER = 0x11e1c78,
    PREPARE_INSERT = 0xc187d0,
    PREPARE_AUTOMATION_UNDO = 0xc17ea0,
    ARRANGEMENTS = 0x156eb60,
    CURRENT_ARRANGEMENT = 0x11d65f0,
    SET_CURRENT_ARRANGEMENT = 0xefb5f0,
    PROJECT_CHANGED_FLAG = 0x11e0bd0,
    NOTIFY_PROJECT_CHANGE = 0x7d68e0,
    UPDATE_PROJECT_CHANGE = 0xdd5560,
    PLAYLIST_FORM_GLOBAL = 0x11e35c8,
    REPAINT_PLAYLIST = 0xaab170,
    CHANNEL_LIST = 0x11e2218,
    INSERT_CHANNEL = 0xc04230,
    PREPARE_CHANNEL_INSERT = 0xc18710,
    /* Bytes 57 56 53 48 83 EC 20 48 89 CB C7 43 48 00 00 00 00, unique in .text. */
    RECOUNT_ACTIVE_CLIPS = 0xc60d60,
    /* Prologue 55 56 53 48 83 EC 60 48 8B EC 66 44 ?? ... unique in .text. */
    SET_CLIP_SOURCE_RANGE = 0xc646d0,
    /* Compact helper called by the engine's own invalid-clip cleanup
       (0x1060930 marks record source -1 and calls this). */
    COMPACT_CLIPS = 0xedf5b0,
    AUTOMATION_LINK_REGISTRY = 0x11e0a00,
    /* FL's create-automation-for-control routine. The isolated twin-automation
       fixture verified the call form (target event, 0, NULL, 0, 1): FL appends
       one automation channel whose control event links to the target event. */
    CREATE_AUTOMATION = 0xd91a50,
    AUTOMATION_ARRAY_TYPE = 0x797100,
    DELPHI_DYNARRAY_SET_LENGTH = 0x17fc0,
    /* Static per-pattern records, stride 0xC0, indexed by the 1-based pattern
       number; the note-recorder collection sits at record+0x28. */
    PATTERN_RECORD_ARRAY = 0x153fce0,
    /* Engine note primitives: the record-note alias appends a complete
       24-byte note; the rebuild re-derives pattern length and playlist clips. */
    RECORD_NOTE_ON = 0xc60320,
    REBUILD_PATTERN = 0xed17a0,
    /* Timeline markers: engine add routine (form, name string, tick, flag). */
    MARKER_ADD = 0xa546a0,
    /* Arrangement management (verified against the uniform +0xFED70 shift
       from the reference build and confirmed by disassembly). */
    ARRANGEMENT_COUNT = 0xef9f10,
    ARRANGEMENT_ADD = 0xef9930,
    ARRANGEMENT_COPY_INTO = 0xefa190,
    ARRANGEMENT_DELETE = 0xef9f30,
    ARRANGEMENT_SET_NAME = 0xef9e40,
    LOAD_IN_PROGRESS_SLOT = 0x11e0ff0,
    /* FLP writer worker (bypasses the Save wrapper's modal error path). */
    PROJECT_SAVE_WORKER = 0xb54570,
};

/* Observed entry bytes for independently verified functions. 0x?? entries are
 * wildcards, written as a separate mask so the comparison stays explicit.
 * These are the signatures recorded when the RVA was first identified; a
 * mismatch means the live engine image is not the one this profile describes.
 * The three FX/save-write entries are resolved from a vtable at runtime and
 * have no static byte constant in this build, so they stay build-gated. */
static const unsigned char recount_clips_bytes[17] = {
    0x57, 0x56, 0x53, 0x48, 0x83, 0xEC, 0x20, 0x48,
    0x89, 0xCB, 0xC7, 0x43, 0x48, 0x00, 0x00, 0x00, 0x00,
};
static const unsigned char set_clip_range_bytes[12] = {
    0x55, 0x56, 0x53, 0x48, 0x83, 0xEC, 0x60, 0x48,
    0x8B, 0xEC, 0x66, 0x44,
};


#endif
