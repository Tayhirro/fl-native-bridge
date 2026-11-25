#include "native.h"
#include "profiles/fl_26_1_7_5654.h"
#include <stdio.h>
#include <string.h>

enum {
    CLIP_PREFIX_SIZE = 0x24,
    MAX_PAGE_SIZE = 64,
    /* Keep identity checks bounded on FL's UI thread. */
    MAX_IDENTITY_SCAN = 8192,
};

static int read_checked(const void *address, void *out, size_t size) {
    MEMORY_BASIC_INFORMATION info;
    if (!address || !VirtualQuery(address, &info, sizeof(info)) ||
        info.State != MEM_COMMIT || (info.Protect & (PAGE_NOACCESS | PAGE_GUARD))) {
        return 0;
    }
    uintptr_t offset = (uintptr_t)address - (uintptr_t)info.BaseAddress;
    if (offset > info.RegionSize || size > info.RegionSize - offset) {
        return 0;
    }
    memcpy(out, address, size);
    return 1;
}

typedef struct {
    unsigned char *engine;
    unsigned char *object;
    unsigned char *data;
    int32_t current;
    int32_t count;
    int32_t stride;
} PlaylistCollection;

static int channel_type(const PlaylistCollection *collection, uint32_t source);
static int channel_is_sampler(const PlaylistCollection *collection, uint32_t source);
static void playlist_commit_refresh(const PlaylistCollection *collection, void *form,
                                    int32_t *changed_flag, int32_t changed_value);

static int playlist_collection(PyObject *self, PlaylistCollection *result) {
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    void *thread_address = NULL;
    DWORD main_thread;
    if (!engine || engine != (unsigned char *)GetModuleHandleW(L"FLEngine_x64.dll") ||
        !read_checked(engine + MAIN_THREAD, &thread_address, sizeof(thread_address)) ||
        !read_checked(thread_address, &main_thread, sizeof(main_thread)) ||
        main_thread != GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError, "Read the verified Playlist on the FL idle thread");
        return 0;
    }

    unsigned char *arrangements = NULL, *arrangement = NULL, *collection = NULL, *data = NULL;
    int32_t current, stride, count;
    if (!read_checked(engine + ARRANGEMENTS, &arrangements, sizeof(arrangements)) ||
        !arrangements ||
        !read_checked(engine + CURRENT_ARRANGEMENT, &current, sizeof(current)) ||
        current < 0 || current > 999 ||
        !read_checked(arrangements + current * sizeof(void *), &arrangement, sizeof(arrangement)) ||
        !arrangement ||
        !read_checked(arrangement + 0x14, &collection, sizeof(collection)) || !collection ||
        !read_checked(collection + 8, &data, sizeof(data)) ||
        !read_checked(collection + 0x10, &stride, sizeof(stride)) ||
        !read_checked(collection + 0x14, &count, sizeof(count)) ||
        stride != 88 || count < 0 || count > 1000000 || (count && !data)) {
        PyErr_SetString(PyExc_RuntimeError, "Playlist collection failed layout validation");
        return 0;
    }

    result->engine = engine;
    result->object = collection;
    result->data = data;
    result->current = current;
    result->count = count;
    result->stride = stride;
    return 1;
}

PyObject *playlist_read_clips(PyObject *self, PyObject *args) {
    int offset, limit;
    if (!PyArg_ParseTuple(args, "ii:read_clips", &offset, &limit)) {
        return NULL;
    }
    if (offset < 0 || limit < 1 || limit > MAX_PAGE_SIZE) {
        PyErr_SetString(PyExc_ValueError, "Invalid Playlist page");
        return NULL;
    }
    PlaylistCollection collection;
    if (!playlist_collection(self, &collection)) {
        return NULL;
    }

    unsigned char records[MAX_PAGE_SIZE * CLIP_PREFIX_SIZE];
    int available = offset < collection.count ? collection.count - offset : 0;
    int take = available < limit ? available : limit;
    for (int i = 0; i < take; i++) {
        uintptr_t record_offset = (uintptr_t)(offset + i) * (uintptr_t)collection.stride;
        if (!read_checked(collection.data + record_offset, records + i * CLIP_PREFIX_SIZE,
                          CLIP_PREFIX_SIZE)) {
            PyErr_SetString(PyExc_RuntimeError, "Cannot read the requested Playlist page");
            return NULL;
        }
    }
    PyObject *bytes = PyBytes_FromStringAndSize((const char *)records, take * CLIP_PREFIX_SIZE);
    if (!bytes) {
        return NULL;
    }
    return Py_BuildValue("(iiiN)", collection.current, collection.count, offset, bytes);
}

PyObject *playlist_move_clip(PyObject *self, PyObject *args) {
    int arrangement, index, start_tick, track, commit;
    const char *expected;
    Py_ssize_t expected_size;
    if (!PyArg_ParseTuple(args, "iiy#iii:move_clip", &arrangement, &index,
                          &expected, &expected_size, &start_tick, &track, &commit)) {
        return NULL;
    }
    if (index < 0 || expected_size != CLIP_PREFIX_SIZE || start_tick < 0 ||
        track < 1 || track > 500 || (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid Playlist clip move");
        return NULL;
    }
    PlaylistCollection collection;
    if (!playlist_collection(self, &collection)) {
        return NULL;
    }
    if (arrangement != collection.current || index >= collection.count) {
        PyErr_SetString(PyExc_ValueError, "The Playlist arrangement or clip index changed");
        return NULL;
    }
    unsigned char *clip = collection.data + (uintptr_t)index * (uintptr_t)collection.stride;
    unsigned char actual[CLIP_PREFIX_SIZE];
    if (!read_checked(clip, actual, sizeof(actual)) || memcmp(actual, expected, sizeof(actual))) {
        PyErr_SetString(PyExc_ValueError, "The Playlist clip changed; read it again");
        return NULL;
    }
    /* Index and visible fields cannot distinguish two identical records. Refuse
       such a move rather than guessing which instance the caller intended. */
    if (collection.count > MAX_IDENTITY_SCAN) {
        PyErr_SetString(PyExc_ValueError, "Playlist too large for a bounded identity check");
        return NULL;
    }
    for (int i = 0; i < collection.count; i++) {
        unsigned char candidate[CLIP_PREFIX_SIZE];
        if (i == index) {
            continue;
        }
        unsigned char *other = collection.data + (uintptr_t)i * (uintptr_t)collection.stride;
        if (!read_checked(other, candidate, sizeof(candidate))) {
            PyErr_SetString(PyExc_RuntimeError, "Cannot validate Playlist clip identity");
            return NULL;
        }
        if (!memcmp(candidate, expected, sizeof(candidate))) {
            PyErr_SetString(PyExc_ValueError, "Ambiguous Playlist snapshot: identical clips exist");
            return NULL;
        }
    }
    int32_t *changed_flag = NULL, changed_value;
    if (!read_checked(collection.engine + PROJECT_CHANGED_FLAG, &changed_flag,
                      sizeof(changed_flag)) ||
        !read_checked(changed_flag, &changed_value, sizeof(changed_value))) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its project change state");
        return NULL;
    }
    MEMORY_BASIC_INFORMATION changed_info;
    if (!VirtualQuery(changed_flag, &changed_info, sizeof(changed_info)) ||
        !(changed_info.Protect & (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE |
                                  PAGE_EXECUTE_WRITECOPY))) {
        PyErr_SetString(PyExc_RuntimeError, "The project change state is not writable");
        return NULL;
    }
    void *form_slot = NULL, *form = NULL, *form_vtable = NULL;
    if (!read_checked(collection.engine + PLAYLIST_FORM_GLOBAL, &form_slot, sizeof(form_slot)) ||
        !read_checked(form_slot, &form, sizeof(form)) ||
        !read_checked(form, &form_vtable, sizeof(form_vtable)) || !form_vtable) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its Playlist refresh object");
        return NULL;
    }
    if (!commit) {
        return PyLong_FromUnsignedLong(0);
    }
    MEMORY_BASIC_INFORMATION info;
    if (!VirtualQuery(clip, &info, sizeof(info)) ||
        !(info.Protect & (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE |
                          PAGE_EXECUTE_WRITECOPY))) {
        PyErr_SetString(PyExc_RuntimeError, "The Playlist clip is not writable");
        return NULL;
    }
    int16_t track_field = (int16_t)(500 - track);
    memcpy(clip, &start_tick, sizeof(start_tick));
    memcpy(clip + 12, &track_field, sizeof(track_field));
    void (*repaint)(void *) = (void (*)(void *))(collection.engine + REPAINT_PLAYLIST);
    repaint(form);
    void (*refresh)(int) = (void (*)(int))(collection.engine + SET_CURRENT_ARRANGEMENT);
    refresh(collection.current);
    if (changed_value != 1) {
        int32_t dirty = 1;
        memcpy(changed_flag, &dirty, sizeof(dirty));
        void (*notify)(int, int) = (void (*)(int, int))(collection.engine + NOTIFY_PROJECT_CHANGE);
        notify(7, 0);
    }
    void (*update_changed)(void) = (void (*)(void))(collection.engine + UPDATE_PROJECT_CHANGE);
    update_changed();
    return PyLong_FromUnsignedLong(0);
}

PyObject *playlist_insert_clip(PyObject *self, PyObject *args) {
    int arrangement, start_tick, length_tick, track, source_start, source_end;
    int expected_count, commit;
    unsigned int source;
    if (!PyArg_ParseTuple(args, "iiIiiiiii:insert_clip", &arrangement, &start_tick, &source,
                          &length_tick, &track, &source_start, &source_end,
                          &expected_count, &commit)) {
        return NULL;
    }
    if (arrangement < 0 || start_tick < 0 || length_tick < 1 || length_tick > 0x3fffffff ||
        track < 1 || track > 500 || expected_count < 0 || expected_count > 100000 ||
        (commit != 0 && commit != 1) ||
        !((source_start == -1 && source_end == -1) ||
          (source_start >= 0 && source_end > source_start && source_end <= 0x7fffffff &&
           length_tick == source_end - source_start))) {
        PyErr_SetString(PyExc_ValueError, "Invalid Playlist clip insertion");
        return NULL;
    }
    /* The source-range setter sizes the clip to the window it receives, so an
       explicit window also fixes the clip length. */
    int effective_length = source_start == -1 ? length_tick : source_end - source_start;
    PlaylistCollection collection;
    if (!playlist_collection(self, &collection)) {
        return NULL;
    }
    if (arrangement != collection.current || collection.count != expected_count) {
        PyErr_SetString(PyExc_ValueError, "The Playlist arrangement or clip count changed");
        return NULL;
    }

    /* The collection object holds the engine's own clip-list vtable. Use its
       initializer and insert entry rather than writing the backing array by
       hand, so FL performs its internal bookkeeping for the new record. */
    void **vtable = NULL, *init_entry = NULL, *insert_entry = NULL;
    if (!read_checked(collection.object, &vtable, sizeof(vtable)) || !vtable ||
        !read_checked(vtable + 1, &insert_entry, sizeof(insert_entry)) || !insert_entry ||
        !read_checked(vtable + 2, &init_entry, sizeof(init_entry)) || !init_entry) {
        PyErr_SetString(PyExc_RuntimeError, "Playlist collection has no usable clip vtable");
        return NULL;
    }
    uintptr_t text_start = (uintptr_t)collection.engine + TEXT_START;
    uintptr_t text_end = (uintptr_t)collection.engine + TEXT_END;
    if ((uintptr_t)init_entry < text_start || (uintptr_t)init_entry >= text_end ||
        (uintptr_t)insert_entry < text_start || (uintptr_t)insert_entry >= text_end) {
        PyErr_SetString(PyExc_RuntimeError, "Playlist clip functions are not in the verified engine text");
        return NULL;
    }
    void *form_slot = NULL, *form = NULL, *form_vtable = NULL;
    if (!read_checked(collection.engine + PLAYLIST_FORM_GLOBAL, &form_slot, sizeof(form_slot)) ||
        !read_checked(form_slot, &form, sizeof(form)) ||
        !read_checked(form, &form_vtable, sizeof(form_vtable)) || !form_vtable) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its Playlist refresh object");
        return NULL;
    }
    int32_t *changed_flag = NULL, changed_value;
    if (!read_checked(collection.engine + PROJECT_CHANGED_FLAG, &changed_flag,
                      sizeof(changed_flag)) ||
        !read_checked(changed_flag, &changed_value, sizeof(changed_value))) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its project change state");
        return NULL;
    }
    if (!commit) {
        return Py_BuildValue("(ii)", -1, collection.count);
    }

    unsigned char record[0x48];
    memset(record, 0, sizeof(record));
    /* The initializer zeroes the whole record first, so every caller field
       must be written after it. Pattern clips additionally need their integer
       -1 window markers restored, because the initializer saw source 0 and
       wrote the audio-style float -1 markers instead. */
    ((void (*)(void *, void *))init_entry)(collection.object, record);
    memcpy(record + 0x00, &start_tick, sizeof(start_tick));
    memcpy(record + 0x04, &source, sizeof(source));
    memcpy(record + 0x08, &effective_length, sizeof(effective_length));
    if (source >= 0x50000000u) {
        static const unsigned char pattern_window[8] = {
            0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
        memcpy(record + 0x18, pattern_window, sizeof(pattern_window));
    }
    int16_t track_field = (int16_t)(500 - track);
    memcpy(record + 0x0c, &track_field, sizeof(track_field));
    /* -1 keeps the source window that init derived from the owning channel. */
    if (source_start != -1) {
        memcpy(record + 0x18, &source_start, sizeof(source_start));
        memcpy(record + 0x1c, &source_end, sizeof(source_end));
    }
    int index = ((int (*)(void *, void *))insert_entry)(collection.object, record);
    if (index < 0 || index > collection.count) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not return a valid inserted clip index");
        return NULL;
    }
    ((void (*)(void *))(collection.engine + RECOUNT_ACTIVE_CLIPS))(collection.object);

    /* FL derives a new clip's length from its source. Pin the requested length
       through the source-range setter first, so later refresh passes re-derive
       the same value instead of the source's own duration. */
    uintptr_t set_range = (uintptr_t)collection.engine + SET_CLIP_SOURCE_RANGE;
    if (set_range < text_start || set_range >= text_end) {
        PyErr_SetString(PyExc_RuntimeError, "The clip source-range setter is not in verified engine text");
        return NULL;
    }
    unsigned char *fresh_data = NULL;
    int32_t fresh_stride = 0, fresh_count = 0;
    if (!read_checked(collection.object + 8, &fresh_data, sizeof(fresh_data)) ||
        !read_checked(collection.object + 0x10, &fresh_stride, sizeof(fresh_stride)) ||
        !read_checked(collection.object + 0x14, &fresh_count, sizeof(fresh_count)) ||
        fresh_stride != 88 || fresh_count != collection.count + 1 || !fresh_data ||
        index >= fresh_count) {
        PyErr_SetString(PyExc_RuntimeError, "The inserted clip address could not be read");
        return NULL;
    }
    unsigned char *clip = fresh_data + (uintptr_t)index * (uintptr_t)fresh_stride;
    int owning_type = channel_type(&collection, source);
    if (source < 0x50000000u && owning_type == 0) {
        int range_start = source_start == -1 ? 0 : source_start;
        int range_end = source_start == -1 ? length_tick : source_end;
        /* Sampler-backed audio clips only: the setter takes the window as
           doubles (XMM1/XMM2), in ticks, and pins the length to the window.
           Pattern clips keep integer -1 window markers; automation clips
           ignore both the window and an explicit length because FL derives
           their extent from the automation envelope. */
        ((void (*)(void *, double, double))set_range)(clip, (double)range_start,
                                                      (double)range_end);
    }
    memcpy(clip + 0x08, &effective_length, sizeof(effective_length));

    void (*repaint)(void *) = (void (*)(void *))(collection.engine + REPAINT_PLAYLIST);
    repaint(form);
    void (*refresh)(int) = (void (*)(int))(collection.engine + SET_CURRENT_ARRANGEMENT);
    refresh(collection.current);
    if (changed_value != 1) {
        int32_t dirty = 1;
        memcpy(changed_flag, &dirty, sizeof(dirty));
        void (*notify)(int, int) = (void (*)(int, int))(collection.engine + NOTIFY_PROJECT_CHANGE);
        notify(7, 0);
    }
    void (*update_changed)(void) = (void (*)(void))(collection.engine + UPDATE_PROJECT_CHANGE);
    update_changed();

    /* An insert may reallocate the backing array, so re-read the collection
       fields and confirm the new record instead of trusting the old pointer. */
    unsigned char *data = NULL;
    int32_t count = 0, stride = 0;
    unsigned char observed[CLIP_PREFIX_SIZE];
    int confirmed = read_checked(collection.object + 8, &data, sizeof(data)) &&
                    read_checked(collection.object + 0x10, &stride, sizeof(stride)) &&
                    read_checked(collection.object + 0x14, &count, sizeof(count)) &&
                    stride == 88 && count == collection.count + 1 && data &&
                    read_checked(data + (uintptr_t)index * (uintptr_t)stride, observed,
                                 sizeof(observed));
    if (confirmed) {
        /* The source-range setter may encode the window in its own units, so the
           caller's fields at +0x18/+0x1c are not compared byte for byte. */
        confirmed = !memcmp(observed + 0x00, &start_tick, sizeof(start_tick)) &&
                    !memcmp(observed + 0x04, &source, sizeof(source)) &&
                    !memcmp(observed + 0x0c, &track_field, sizeof(track_field));
        /* FL derives an automation clip's extent from its envelope during the
           refresh, so the requested length cannot be confirmed for them. This
           build exposes two automation kinds: legacy clips read 2 and current
           CT_AutoClip channels read 5 (same value as the internal kind that
           the automation readers require). */
        if (confirmed && owning_type != 2 && owning_type != 5) {
            confirmed = !memcmp(observed + 0x08, &effective_length,
                                sizeof(effective_length));
        }
        if (confirmed && source >= 0x50000000u) {
            static const unsigned char pattern_window[8] = {
                0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
            confirmed = !memcmp(observed + 0x18, pattern_window, sizeof(pattern_window));
        }
    }
    if (!confirmed) {
#ifdef FLMCP_DEBUG_INSERT
        char detail[256];
        int used = snprintf(detail, sizeof(detail),
                            "index=%d old=%d count=%d stride=%d data=%p obs=",
                            index, collection.count, count, stride, (void *)data);
        for (int i = 0; i < CLIP_PREFIX_SIZE && used < (int)sizeof(detail) - 4; i++) {
            used += snprintf(detail + used, sizeof(detail) - used, "%02x", observed[i]);
        }
        PyErr_SetString(PyExc_RuntimeError, detail);
#else
        PyErr_SetString(PyExc_RuntimeError,
                        "A clip was inserted but its record could not be confirmed");
#endif
        return NULL;
    }
    return Py_BuildValue("(ii)", index, count);
}

/* Read the target record's 36-byte prefix and refuse when any other record
   matches it, mirroring the move path's identity discipline. Also refuses
   records the engine already invalidated (source == -1). */
static int locate_target_clip(const PlaylistCollection *collection, int index,
                              const char *expected, unsigned char *observed) {
    if (index >= collection->count) {
        PyErr_SetString(PyExc_ValueError, "The Playlist arrangement or clip index changed");
        return 0;
    }
    unsigned char *clip = collection->data + (uintptr_t)index * (uintptr_t)collection->stride;
    if (!read_checked(clip, observed, CLIP_PREFIX_SIZE) ||
        memcmp(observed, expected, CLIP_PREFIX_SIZE)) {
        PyErr_SetString(PyExc_ValueError, "The Playlist clip changed; read it again");
        return 0;
    }
    {
        uint32_t target_source;
        memcpy(&target_source, observed + 4, sizeof(target_source));
        if (target_source == 0xffffffffu) {
            PyErr_SetString(PyExc_ValueError, "The target clip record is already invalidated");
            return 0;
        }
    }
    if (collection->count > MAX_IDENTITY_SCAN) {
        PyErr_SetString(PyExc_ValueError, "Playlist too large for a bounded identity check");
        return 0;
    }
    for (int i = 0; i < collection->count; i++) {
        if (i == index) {
            continue;
        }
        unsigned char candidate[CLIP_PREFIX_SIZE];
        unsigned char *other = collection->data + (uintptr_t)i * (uintptr_t)collection->stride;
        if (!read_checked(other, candidate, sizeof(candidate))) {
            PyErr_SetString(PyExc_RuntimeError, "Cannot validate Playlist clip identity");
            return 0;
        }
        if (!memcmp(candidate, expected, sizeof(candidate))) {
            PyErr_SetString(PyExc_ValueError, "Ambiguous Playlist snapshot: identical clips exist");
            return 0;
        }
        uint32_t other_source;
        memcpy(&other_source, candidate + 4, sizeof(other_source));
        if (other_source == 0xffffffffu) {
            PyErr_SetString(PyExc_RuntimeError,
                            "The Playlist already contains an invalidated clip record");
            return 0;
        }
    }
    return 1;
}

/* Collect FL's project-dirty state and Playlist form handle shared by every
   commit path. Returns 0 with a Python exception set on failure. */
static int gather_commit_objects(const PlaylistCollection *collection,
                                 void **form, int32_t **changed_flag, int32_t *changed_value) {
    if (!read_checked(collection->engine + PROJECT_CHANGED_FLAG, changed_flag,
                      sizeof(*changed_flag)) ||
        !read_checked(*changed_flag, changed_value, sizeof(*changed_value))) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its project change state");
        return 0;
    }
    void *form_slot = NULL, *form_vtable = NULL;
    if (!read_checked(collection->engine + PLAYLIST_FORM_GLOBAL, &form_slot, sizeof(form_slot)) ||
        !read_checked(form_slot, form, sizeof(*form)) ||
        !read_checked(*form, &form_vtable, sizeof(form_vtable)) || !form_vtable) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its Playlist refresh object");
        return 0;
    }
    return 1;
}

/* Read the owning channel's type. Samplers are 0; automation clips are 2
   (legacy) or 5 (CT_AutoClip); other generator types must not receive audio
   source-window writes. Returns -1 when the channel list is unreadable. */
static int channel_type(const PlaylistCollection *collection, uint32_t source) {
    unsigned char *slot = NULL, *list = NULL, **items = NULL, *channel = NULL;
    int32_t count = 0;
    int index = (int)(source >> 16);
    if (!read_checked(collection->engine + CHANNEL_LIST, &slot, sizeof(slot)) || !slot ||
        !read_checked(slot, &list, sizeof(list)) || !list ||
        !read_checked(list + 0x10, &count, sizeof(count)) ||
        index < 0 || index >= count ||
        !read_checked(list + 8, &items, sizeof(items)) || !items ||
        !read_checked(items + index, &channel, sizeof(channel)) || !channel) {
        return -1;
    }
    int32_t type = -1;
    if (!read_checked(channel + 0x160, &type, sizeof(type))) {
        return -1;
    }
    return type;
}

static int channel_is_sampler(const PlaylistCollection *collection, uint32_t source) {
    int type = channel_type(collection, source);
    return type == 0;
}

/* Repaint, reselect the arrangement, flag the project dirty when FL has not
   already, and push the change notification. Call only on the idle thread
   after a committed edit. */
static void playlist_commit_refresh(const PlaylistCollection *collection, void *form,
                                    int32_t *changed_flag, int32_t changed_value) {
    void (*repaint)(void *) = (void (*)(void *))(collection->engine + REPAINT_PLAYLIST);
    repaint(form);
    void (*refresh)(int) = (void (*)(int))(collection->engine + SET_CURRENT_ARRANGEMENT);
    refresh(collection->current);
    if (changed_value != 1) {
        int32_t dirty = 1;
        memcpy(changed_flag, &dirty, sizeof(dirty));
        void (*notify)(int, int) = (void (*)(int, int))(collection->engine + NOTIFY_PROJECT_CHANGE);
        notify(7, 0);
    }
    void (*update_changed)(void) = (void (*)(void))(collection->engine + UPDATE_PROJECT_CHANGE);
    update_changed();
}

static int require_writable(const void *address, const char *message) {
    MEMORY_BASIC_INFORMATION info;
    if (!VirtualQuery(address, &info, sizeof(info)) ||
        !(info.Protect & (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE |
                          PAGE_EXECUTE_WRITECOPY))) {
        PyErr_SetString(PyExc_RuntimeError, message);
        return 0;
    }
    return 1;
}

PyObject *playlist_delete_clip(PyObject *self, PyObject *args) {
    int arrangement, index, commit;
    const char *expected;
    Py_ssize_t expected_size;
    if (!PyArg_ParseTuple(args, "iiy#i:delete_clip", &arrangement, &index,
                          &expected, &expected_size, &commit)) {
        return NULL;
    }
    if (index < 0 || expected_size != CLIP_PREFIX_SIZE || (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid Playlist clip deletion");
        return NULL;
    }
    PlaylistCollection collection;
    if (!playlist_collection(self, &collection)) {
        return NULL;
    }
    if (arrangement != collection.current) {
        PyErr_SetString(PyExc_ValueError, "The Playlist arrangement or clip index changed");
        return NULL;
    }
    unsigned char observed[CLIP_PREFIX_SIZE];
    if (!locate_target_clip(&collection, index, expected, observed)) {
        return NULL;
    }
    void *form;
    int32_t *changed_flag, changed_value;
    if (!gather_commit_objects(&collection, &form, &changed_flag, &changed_value)) {
        return NULL;
    }
    if (!commit) {
        return PyLong_FromLong(collection.count - 1);
    }
    if (!require_writable(collection.data + (uintptr_t)index * (uintptr_t)collection.stride,
                          "The Playlist clip is not writable")) {
        return NULL;
    }
    uintptr_t compact = (uintptr_t)collection.engine + COMPACT_CLIPS;
    if (compact < (uintptr_t)collection.engine + TEXT_START ||
        compact >= (uintptr_t)collection.engine + TEXT_END) {
        PyErr_SetString(PyExc_RuntimeError, "The Playlist compact helper is not in verified engine text");
        return NULL;
    }
    /* Mirror the engine's own cleanup (0x1060930): invalidate the record, then
       let FL compact the array and adjust its counters. */
    uint32_t dead = 0xffffffffu;
    unsigned char *clip = collection.data + (uintptr_t)index * (uintptr_t)collection.stride;
    memcpy(clip + 0x04, &dead, sizeof(dead));
    ((void (*)(void *))compact)(collection.object);
    ((void (*)(void *))(collection.engine + RECOUNT_ACTIVE_CLIPS))(collection.object);
    playlist_commit_refresh(&collection, form, changed_flag, changed_value);

    /* Deletion must shrink the array by exactly one; compact does not
       reallocate, so the previously validated pointers stay readable. */
    int32_t count = 0;
    int confirmed = read_checked(collection.object + 0x14, &count, sizeof(count)) &&
                    count == collection.count - 1;
    if (confirmed) {
        for (int i = 0; i < count; i++) {
            unsigned char candidate[CLIP_PREFIX_SIZE];
            unsigned char *other = collection.data + (uintptr_t)i * (uintptr_t)collection.stride;
            if (!read_checked(other, candidate, sizeof(candidate))) {
                confirmed = 0;
                break;
            }
            if (!memcmp(candidate, expected, sizeof(candidate))) {
                confirmed = 0;
                break;
            }
        }
    }
    if (!confirmed) {
        PyErr_SetString(PyExc_RuntimeError,
                        "The clip was invalidated but FL did not confirm the deletion");
        return NULL;
    }
    return Py_BuildValue("(ii)", index, count);
}

PyObject *playlist_duplicate_clip(PyObject *self, PyObject *args) {
    int arrangement, index, start_tick, track, commit;
    const char *expected;
    Py_ssize_t expected_size;
    if (!PyArg_ParseTuple(args, "iiy#iii:duplicate_clip", &arrangement, &index,
                          &expected, &expected_size, &start_tick, &track, &commit)) {
        return NULL;
    }
    if (index < 0 || expected_size != CLIP_PREFIX_SIZE || start_tick < 0 ||
        track < 1 || track > 500 || (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid Playlist clip duplication");
        return NULL;
    }
    PlaylistCollection collection;
    if (!playlist_collection(self, &collection)) {
        return NULL;
    }
    if (arrangement != collection.current) {
        PyErr_SetString(PyExc_ValueError, "The Playlist arrangement or clip index changed");
        return NULL;
    }
    unsigned char observed[CLIP_PREFIX_SIZE];
    if (!locate_target_clip(&collection, index, expected, observed)) {
        return NULL;
    }
    void **vtable = NULL, *insert_entry = NULL;
    if (!read_checked(collection.object, &vtable, sizeof(vtable)) || !vtable ||
        !read_checked(vtable + 1, &insert_entry, sizeof(insert_entry)) || !insert_entry) {
        PyErr_SetString(PyExc_RuntimeError, "Playlist collection has no usable clip vtable");
        return NULL;
    }
    uintptr_t text_start = (uintptr_t)collection.engine + TEXT_START;
    uintptr_t text_end = (uintptr_t)collection.engine + TEXT_END;
    if ((uintptr_t)insert_entry < text_start || (uintptr_t)insert_entry >= text_end) {
        PyErr_SetString(PyExc_RuntimeError, "Playlist clip functions are not in the verified engine text");
        return NULL;
    }
    void *form;
    int32_t *changed_flag, changed_value;
    if (!gather_commit_objects(&collection, &form, &changed_flag, &changed_value)) {
        return NULL;
    }
    if (!commit) {
        return Py_BuildValue("(ii)", -1, collection.count);
    }

    /* The engine's insert entry copies the whole 88-byte record, so a byte
       copy of the source record carries the source window, appearance, and
       embedded scalar structures without aliasing any pointer. FL assigns the
       new record its own identifier at +0x20 during insertion. */
    unsigned char *source = collection.data + (uintptr_t)index * (uintptr_t)collection.stride;
    if (!require_writable(source, "The Playlist clip record is not accessible")) {
        return NULL;
    }
    unsigned char record[88];
    if (!read_checked(source, record, sizeof(record))) {
        PyErr_SetString(PyExc_RuntimeError, "Cannot read the full source clip record");
        return NULL;
    }
    uint32_t source_id;
    memcpy(&source_id, record + 0x04, sizeof(source_id));
    if (source_id == 0xffffffffu) {
        PyErr_SetString(PyExc_ValueError, "The source clip record is invalidated");
        return NULL;
    }
    memcpy(record + 0x00, &start_tick, sizeof(start_tick));
    int16_t track_field = (int16_t)(500 - track);
    memcpy(record + 0x0c, &track_field, sizeof(track_field));
    record[0x13] = (unsigned char)(record[0x13] & 0x7f);
    int new_index = ((int (*)(void *, void *))insert_entry)(collection.object, record);
    if (new_index < 0 || new_index > collection.count) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not return a valid inserted clip index");
        return NULL;
    }
    ((void (*)(void *))(collection.engine + RECOUNT_ACTIVE_CLIPS))(collection.object);
    playlist_commit_refresh(&collection, form, changed_flag, changed_value);

    unsigned char *data = NULL;
    int32_t count = 0, stride = 0;
    unsigned char fresh[CLIP_PREFIX_SIZE];
    int confirmed = read_checked(collection.object + 8, &data, sizeof(data)) &&
                    read_checked(collection.object + 0x10, &stride, sizeof(stride)) &&
                    read_checked(collection.object + 0x14, &count, sizeof(count)) &&
                    stride == 88 && count == collection.count + 1 && data &&
                    new_index < count &&
                    read_checked(data + (uintptr_t)new_index * (uintptr_t)stride, fresh,
                                 sizeof(fresh));
    if (confirmed) {
        confirmed = !memcmp(fresh + 0x00, &start_tick, sizeof(start_tick)) &&
                    !memcmp(fresh + 0x04, record + 0x04, sizeof(source_id)) &&
                    !memcmp(fresh + 0x08, record + 0x08, 4) &&
                    !memcmp(fresh + 0x0c, &track_field, sizeof(track_field)) &&
                    !(fresh[0x13] & 0x80) &&
                    !memcmp(fresh + 0x18, record + 0x18, 8) &&
                    memcmp(fresh + 0x20, record + 0x20, 4) != 0;
    }
    if (!confirmed) {
        PyErr_SetString(PyExc_RuntimeError,
                        "A clip was duplicated but its record could not be confirmed");
        return NULL;
    }
    return Py_BuildValue("(ii)", new_index, count);
}

PyObject *playlist_set_clip_muted(PyObject *self, PyObject *args) {
    int arrangement, index, muted, commit;
    const char *expected;
    Py_ssize_t expected_size;
    if (!PyArg_ParseTuple(args, "iiy#ii:set_clip_muted", &arrangement, &index,
                          &expected, &expected_size, &muted, &commit)) {
        return NULL;
    }
    if (index < 0 || expected_size != CLIP_PREFIX_SIZE || (muted != 0 && muted != 1) ||
        (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid Playlist clip mute request");
        return NULL;
    }
    PlaylistCollection collection;
    if (!playlist_collection(self, &collection)) {
        return NULL;
    }
    if (arrangement != collection.current) {
        PyErr_SetString(PyExc_ValueError, "The Playlist arrangement or clip index changed");
        return NULL;
    }
    unsigned char observed[CLIP_PREFIX_SIZE];
    if (!locate_target_clip(&collection, index, expected, observed)) {
        return NULL;
    }
    void *form;
    int32_t *changed_flag, changed_value;
    if (!gather_commit_objects(&collection, &form, &changed_flag, &changed_value)) {
        return NULL;
    }
    unsigned char current = observed[0x13];
    unsigned char wanted = (unsigned char)((current & ~0x20) | (muted ? 0x20 : 0));
    if (!commit || wanted == current) {
        return PyLong_FromLong((current & 0x20) != 0);
    }
    unsigned char *clip = collection.data + (uintptr_t)index * (uintptr_t)collection.stride;
    if (!require_writable(clip, "The Playlist clip is not writable")) {
        return NULL;
    }
    memcpy(clip + 0x13, &wanted, sizeof(wanted));
    playlist_commit_refresh(&collection, form, changed_flag, changed_value);
    unsigned char fresh = 0;
    if (!read_checked(clip + 0x13, &fresh, sizeof(fresh)) ||
        (fresh & 0x20) != (wanted & 0x20)) {
        PyErr_SetString(PyExc_RuntimeError, "The clip mute flag could not be confirmed");
        return NULL;
    }
    return PyLong_FromLong((fresh & 0x20) != 0);
}

PyObject *playlist_trim_clip(PyObject *self, PyObject *args) {
    int arrangement, index, window_start, window_end, commit;
    const char *expected;
    Py_ssize_t expected_size;
    if (!PyArg_ParseTuple(args, "iiy#iii:trim_clip", &arrangement, &index,
                          &expected, &expected_size, &window_start, &window_end, &commit)) {
        return NULL;
    }
    if (index < 0 || expected_size != CLIP_PREFIX_SIZE || window_start < 0 ||
        window_end <= window_start || window_end > 0x7fffffff ||
        (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid Playlist clip trim");
        return NULL;
    }
    int new_length = window_end - window_start;
    if (new_length > 0x3fffffff) {
        PyErr_SetString(PyExc_ValueError, "Invalid Playlist clip trim");
        return NULL;
    }
    PlaylistCollection collection;
    if (!playlist_collection(self, &collection)) {
        return NULL;
    }
    if (arrangement != collection.current) {
        PyErr_SetString(PyExc_ValueError, "The Playlist arrangement or clip index changed");
        return NULL;
    }
    unsigned char observed[CLIP_PREFIX_SIZE];
    if (!locate_target_clip(&collection, index, expected, observed)) {
        return NULL;
    }
    uint32_t source_raw;
    memcpy(&source_raw, observed + 0x04, sizeof(source_raw));
    int is_pattern = source_raw >= 0x50000000u;
    int has_window = !is_pattern && channel_is_sampler(&collection, source_raw);
    void *form;
    int32_t *changed_flag, changed_value;
    if (!gather_commit_objects(&collection, &form, &changed_flag, &changed_value)) {
        return NULL;
    }
    if (!commit) {
        return PyLong_FromLong(new_length);
    }
    unsigned char *clip = collection.data + (uintptr_t)index * (uintptr_t)collection.stride;
    if (!require_writable(clip, "The Playlist clip is not writable")) {
        return NULL;
    }
    if (!has_window) {
        /* Pattern and automation clips have no source window: the visible
           trim is only the clip length. Their window fields stay untouched. */
        memcpy(clip + 0x08, &new_length, sizeof(new_length));
    } else {
        uintptr_t set_range = (uintptr_t)collection.engine + SET_CLIP_SOURCE_RANGE;
        if (set_range < (uintptr_t)collection.engine + TEXT_START ||
            set_range >= (uintptr_t)collection.engine + TEXT_END) {
            PyErr_SetString(PyExc_RuntimeError, "The clip source-range setter is not in verified engine text");
            return NULL;
        }
        /* The setter takes the window as doubles (XMM1/XMM2), in ticks, and
           sizes the clip to the window it receives. */
        ((void (*)(void *, double, double))set_range)(clip, (double)window_start,
                                                      (double)window_end);
        memcpy(clip + 0x08, &new_length, sizeof(new_length));
    }
    playlist_commit_refresh(&collection, form, changed_flag, changed_value);

    unsigned char fresh[CLIP_PREFIX_SIZE];
    if (!read_checked(clip, fresh, sizeof(fresh)) ||
        memcmp(fresh + 0x08, &new_length, sizeof(new_length))) {
        PyErr_SetString(PyExc_RuntimeError, "The clip trim could not be confirmed");
        return NULL;
    }
    return PyBytes_FromStringAndSize((const char *)fresh, CLIP_PREFIX_SIZE);
}
