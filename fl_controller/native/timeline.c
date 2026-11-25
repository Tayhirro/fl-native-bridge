#include "native.h"
#include "profiles/fl_26_1_7_5654.h"
#include <string.h>

/* Timeline markers: the Playlist form keeps the marker manager at +0xD84.
   The manager holds a Delphi dynamic array of 52-byte records (signed 64-bit
   length at data-8); each record stores the tick at +0x00 and a Delphi
   UnicodeString pointer at +0x08. */

enum {
    MARKER_STRIDE = 52,
    MAX_MARKERS = 100000,
    MAX_MARKER_PAGE = 32,
    MAX_NAME_CHARS = 512,
    MANAGER_FORM_OFFSET = 0xD84,
    MARKER_NAME_OFFSET = 0x8,
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

static int on_idle_thread(unsigned char *engine) {
    void *thread_address = NULL;
    DWORD main_thread;
    if (!engine || !read_checked(engine + MAIN_THREAD, &thread_address, sizeof(thread_address)) ||
        !read_checked(thread_address, &main_thread, sizeof(main_thread)) ||
        main_thread != GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError, "Read the verified markers on the FL idle thread");
        return 0;
    }
    return 1;
}

/* Marks the project dirty and notifies FL, mirroring the Playlist edit
   bookkeeping: without the change notification FL treats the undo point as
   empty and undoUp becomes a no-op. */
static int notify_project_change(unsigned char *engine) {
    int32_t *changed_flag = NULL;
    int32_t changed_value = 0;
    if (!read_checked(engine + PROJECT_CHANGED_FLAG, &changed_flag, sizeof(changed_flag)) ||
        !read_checked(changed_flag, &changed_value, sizeof(changed_value))) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its project change state");
        return 0;
    }
    if (changed_value != 1) {
        int32_t dirty = 1;
        memcpy(changed_flag, &dirty, sizeof(dirty));
        void (*notify)(int, int) = (void (*)(int, int))(engine + NOTIFY_PROJECT_CHANGE);
        notify(7, 0);
    }
    void (*update_changed)(void) = (void (*)(void))(engine + UPDATE_PROJECT_CHANGE);
    update_changed();
    return 1;
}

/* Resolves the marker array for the active arrangement. Returns 1 with
   data/count (count 0 when no marker manager or empty array exists). */
static int marker_array(unsigned char *engine, unsigned char **data, long long *count) {
    void *form_slot = NULL, *form = NULL, *manager = NULL;
    if (!read_checked(engine + PLAYLIST_FORM_GLOBAL, &form_slot, sizeof(form_slot)) ||
        !read_checked(form_slot, &form, sizeof(form)) || !form ||
        !read_checked((unsigned char *)form + MANAGER_FORM_OFFSET, &manager,
                      sizeof(manager))) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its marker manager");
        return 0;
    }
    *data = NULL;
    *count = 0;
    if (!manager) {
        return 1;
    }
    unsigned char *array = NULL;
    if (!read_checked(manager, &array, sizeof(array)) || !array) {
        return 1;
    }
    long long length = 0;
    if (!read_checked(array - 8, &length, sizeof(length)) || length < 0 ||
        length > MAX_MARKERS) {
        PyErr_SetString(PyExc_RuntimeError, "The marker array failed layout validation");
        return 0;
    }
    *data = array;
    *count = length;
    return 1;
}

/* Copies a marker's Delphi UnicodeString character data (UTF-16LE). The
   signed 32-bit character length sits at pointer-4 in Delphi strings. */
static int read_marker_name(const unsigned char *record, wchar_t *buffer, size_t *chars) {
    void *name_ptr = NULL;
    if (!read_checked(record + MARKER_NAME_OFFSET, &name_ptr, sizeof(name_ptr))) {
        return 0;
    }
    *chars = 0;
    if (!name_ptr) {
        return 1;
    }
    int32_t length = 0;
    if (!read_checked((unsigned char *)name_ptr - 4, &length, sizeof(length)) ||
        length < 0 || length > MAX_NAME_CHARS) {
        return 0;
    }
    if (length > 0 &&
        !read_checked(name_ptr, buffer, (size_t)length * sizeof(wchar_t))) {
        return 0;
    }
    *chars = (size_t)length;
    return 1;
}

PyObject *timeline_read_markers(PyObject *self, PyObject *args) {
    int offset, limit;
    if (!PyArg_ParseTuple(args, "ii:read_markers", &offset, &limit)) {
        return NULL;
    }
    if (offset < 0 || limit < 1 || limit > MAX_MARKER_PAGE) {
        PyErr_SetString(PyExc_ValueError, "Invalid marker page");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    unsigned char *data = NULL;
    long long count = 0;
    if (!marker_array(engine, &data, &count)) {
        return NULL;
    }
    int take = (offset < count) ? (int)((count - offset < limit) ? (count - offset) : limit) : 0;
    /* Page encoding: per marker, tick (i32 LE) + name length in UTF-16 code
       units (u16 LE) + UTF-16LE character data. */
    unsigned char page[MAX_MARKER_PAGE * (4 + 2 + MAX_NAME_CHARS * 2)];
    size_t used = 0;
    for (int i = 0; i < take; i++) {
        const unsigned char *record = data + (size_t)(offset + i) * MARKER_STRIDE;
        int32_t tick = 0;
        wchar_t buffer[MAX_NAME_CHARS];
        size_t chars = 0;
        if (!read_checked(record, &tick, sizeof(tick)) ||
            !read_marker_name(record, buffer, &chars)) {
            PyErr_SetString(PyExc_RuntimeError, "Cannot read the requested marker page");
            return NULL;
        }
        size_t name_bytes = chars * sizeof(wchar_t);
        if (used + 4 + 2 + name_bytes > sizeof(page)) {
            PyErr_SetString(PyExc_RuntimeError, "Marker page too large");
            return NULL;
        }
        memcpy(page + used, &tick, sizeof(tick));
        unsigned short name_bytes16 = (unsigned short)name_bytes;
        memcpy(page + used + 4, &name_bytes16, sizeof(name_bytes16));
        memcpy(page + used + 6, buffer, name_bytes);
        used += 4 + 2 + name_bytes;
    }
    PyObject *bytes = PyBytes_FromStringAndSize((const char *)page, (Py_ssize_t)used);
    if (!bytes) {
        return NULL;
    }
    return Py_BuildValue("(lN)", count, bytes);
}

/* Adds a named marker through the engine's own add routine. tick is the
   absolute timeline position in PPQ ticks. */
PyObject *timeline_add_marker(PyObject *self, PyObject *args) {
    int tick, commit;
    const char *name;
    if (!PyArg_ParseTuple(args, "isi:add_marker", &tick, &name, &commit)) {
        return NULL;
    }
    if (tick < 0 || tick > 0x7fffffff || (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid marker request");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    void *form_slot = NULL, *form = NULL;
    if (!read_checked(engine + PLAYLIST_FORM_GLOBAL, &form_slot, sizeof(form_slot)) ||
        !read_checked(form_slot, &form, sizeof(form)) || !form) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its Playlist form");
        return NULL;
    }
    unsigned char *data = NULL;
    long long count = 0;
    if (!marker_array(engine, &data, &count)) {
        return NULL;
    }
    if (!commit) {
        return PyLong_FromLong(count + 1);
    }
    void *string = NULL;
    ((void (*)(void **, const char *))(engine + FROM_UTF8))(&string, name);
    if (!string) {
        PyErr_SetString(PyExc_ValueError, "Invalid marker name");
        return NULL;
    }
    void (*add_marker)(void *, void *, int *, unsigned char) =
        (void (*)(void *, void *, int *, unsigned char))(engine + MARKER_ADD);
    /* The engine stores the passed string object itself; it is not copied,
       so the caller must not free it (FL owns and releases it with the
       marker). */
    add_marker(form, &string, &tick, 1);

    if (!notify_project_change(engine)) {
        return NULL;
    }
    int32_t current = 0;
    read_checked(engine + CURRENT_ARRANGEMENT, &current, sizeof(current));
    void (*repaint)(void *) = (void (*)(void *))(engine + REPAINT_PLAYLIST);
    repaint(form);
    void (*refresh)(int) = (void (*)(int))(engine + SET_CURRENT_ARRANGEMENT);
    refresh(current);

    unsigned char *after = NULL;
    long long new_count = 0;
    if (!marker_array(engine, &after, &new_count) || new_count != count + 1) {
        PyErr_SetString(PyExc_RuntimeError, "The marker was added but not confirmed");
        return NULL;
    }
    /* The engine inserts markers sorted by tick; confirm the tick exists. */
    int found = 0;
    for (long long i = 0; i < new_count; i++) {
        int32_t stored = 0;
        if (read_checked(after + (size_t)i * MARKER_STRIDE, &stored, sizeof(stored)) &&
            stored == tick) {
            found = 1;
            break;
        }
    }
    if (!found) {
        PyErr_SetString(PyExc_RuntimeError, "The added marker tick was not confirmed");
        return NULL;
    }
    return PyLong_FromLong(new_count);
}

/* Deletes the marker at an index after confirming its stored tick still
   matches the caller's observation. Follows FL's own array handling: shift
   the tail down, shrink the Delphi length, then refresh the arrangement. */
PyObject *timeline_delete_marker(PyObject *self, PyObject *args) {
    int index, expected_tick, commit;
    if (!PyArg_ParseTuple(args, "iii:delete_marker", &index, &expected_tick, &commit)) {
        return NULL;
    }
    if (index < 0 || expected_tick < 0 || (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid marker deletion");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    unsigned char *data = NULL;
    long long count = 0;
    if (!marker_array(engine, &data, &count)) {
        return NULL;
    }
    if (index >= count) {
        PyErr_SetString(PyExc_ValueError, "The marker index does not exist");
        return NULL;
    }
    const unsigned char *record = data + (size_t)index * MARKER_STRIDE;
    int32_t stored = 0;
    if (!read_checked(record, &stored, sizeof(stored)) || stored != expected_tick) {
        PyErr_SetString(PyExc_ValueError, "The marker changed; read it again");
        return NULL;
    }
    if (!commit) {
        return PyLong_FromLong(count - 1);
    }
    size_t tail = (size_t)(count - 1 - index) * MARKER_STRIDE;
    if (tail > 0) {
        memmove(data + (size_t)index * MARKER_STRIDE,
                data + (size_t)(index + 1) * MARKER_STRIDE, tail);
    }
    long long new_count = count - 1;
    memcpy(data - 8, &new_count, sizeof(new_count));

    if (!notify_project_change(engine)) {
        return NULL;
    }
    void *form_slot = NULL, *form = NULL;
    if (!read_checked(engine + PLAYLIST_FORM_GLOBAL, &form_slot, sizeof(form_slot)) ||
        !read_checked(form_slot, &form, sizeof(form)) || !form) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its Playlist form");
        return NULL;
    }
    int32_t current = 0;
    read_checked(engine + CURRENT_ARRANGEMENT, &current, sizeof(current));
    void (*repaint)(void *) = (void (*)(void *))(engine + REPAINT_PLAYLIST);
    repaint(form);
    void (*refresh)(int) = (void (*)(int))(engine + SET_CURRENT_ARRANGEMENT);
    refresh(current);

    unsigned char *after_data = NULL;
    long long after = 0;
    if (!marker_array(engine, &after_data, &after) || after != new_count) {
        PyErr_SetString(PyExc_RuntimeError, "The marker deletion was not confirmed");
        return NULL;
    }
    return PyLong_FromLong(after);
}

/* Diagnostic/test helper: re-selects the current arrangement and repaints the
   Playlist so form-level views re-read their backing state. */
PyObject *timeline_refresh(PyObject *self, PyObject *unused) {
    (void)unused;
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    void *form_slot = NULL, *form = NULL;
    if (!read_checked(engine + PLAYLIST_FORM_GLOBAL, &form_slot, sizeof(form_slot)) ||
        !read_checked(form_slot, &form, sizeof(form)) || !form) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its Playlist form");
        return NULL;
    }
    int32_t current = 0;
    read_checked(engine + CURRENT_ARRANGEMENT, &current, sizeof(current));
    void (*repaint)(void *) = (void (*)(void *))(engine + REPAINT_PLAYLIST);
    repaint(form);
    void (*refresh)(int) = (void (*)(int))(engine + SET_CURRENT_ARRANGEMENT);
    refresh(current);
    return Py_BuildValue("i", 0);
}


/* ---- Arrangement management ----
   The arrangements global is a Delphi dynamic array of arrangement pointers;
   each arrangement stores its name string at +0x08. All routines here were
   located through a uniform +0xFED70 shift from the reference build and each
   was verified by disassembly before use. */

static int arrangements_array(unsigned char *engine, unsigned char **data, long long *count) {
    unsigned char *array_ptr = NULL;
    if (!read_checked(engine + ARRANGEMENTS, &array_ptr, sizeof(array_ptr)) || !array_ptr) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its arrangements array");
        return 0;
    }
    long long length = 0;
    if (!read_checked(array_ptr - 8, &length, sizeof(length)) || length < 1 ||
        length > 1000) {
        PyErr_SetString(PyExc_RuntimeError, "The arrangements array failed validation");
        return 0;
    }
    *data = array_ptr;
    *count = length;
    return 1;
}

/* Reads an arrangement's Delphi UnicodeString name into UTF-16LE bytes. */
static int read_arrangement_name(unsigned char *arrangement, wchar_t *buffer, size_t *chars) {
    void *name_ptr = NULL;
    if (!read_checked(arrangement + 8, &name_ptr, sizeof(name_ptr))) {
        return 0;
    }
    *chars = 0;
    if (!name_ptr) {
        return 1;
    }
    int32_t length = 0;
    if (!read_checked((unsigned char *)name_ptr - 4, &length, sizeof(length)) ||
        length < 0 || length > MAX_NAME_CHARS) {
        return 0;
    }
    if (length > 0 &&
        !read_checked(name_ptr, buffer, (size_t)length * sizeof(wchar_t))) {
        return 0;
    }
    *chars = (size_t)length;
    return 1;
}

PyObject *arrangement_read_all(PyObject *self, PyObject *args) {
    if (!PyArg_ParseTuple(args, ":read_all")) {
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    unsigned char *data = NULL;
    long long count = 0;
    if (!arrangements_array(engine, &data, &count)) {
        return NULL;
    }
    int32_t current = 0;
    if (!read_checked(engine + CURRENT_ARRANGEMENT, &current, sizeof(current))) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its current arrangement");
        return NULL;
    }
    /* Page encoding per arrangement: index (i32) + name UTF-16 length (u16)
       + UTF-16LE data, for up to the first 64 arrangements. */
    unsigned char page[64 * (4 + 2 + MAX_NAME_CHARS * 2)];
    size_t used = 0;
    for (int i = 0; i < count && i < 64; i++) {
        unsigned char *arrangement = NULL;
        if (!read_checked(data + (size_t)i * 8, &arrangement, sizeof(arrangement)) ||
            !arrangement) {
            PyErr_SetString(PyExc_RuntimeError, "The arrangement list is unreadable");
            return NULL;
        }
        wchar_t buffer[MAX_NAME_CHARS];
        size_t chars = 0;
        if (!read_arrangement_name(arrangement, buffer, &chars)) {
            PyErr_SetString(PyExc_RuntimeError, "Cannot read an arrangement name");
            return NULL;
        }
        size_t name_bytes = chars * sizeof(wchar_t);
        if (used + 4 + 2 + name_bytes > sizeof(page)) {
            break;
        }
        memcpy(page + used, &i, sizeof(i));
        unsigned short name_bytes16 = (unsigned short)name_bytes;
        memcpy(page + used + 4, &name_bytes16, sizeof(name_bytes16));
        memcpy(page + used + 6, buffer, name_bytes);
        used += 4 + 2 + name_bytes;
    }
    PyObject *bytes = PyBytes_FromStringAndSize((const char *)page, (Py_ssize_t)used);
    if (!bytes) {
        return NULL;
    }
    return Py_BuildValue("(iiN)", count, current, bytes);
}

static int arrangement_refresh(unsigned char *engine) {
    void *form_slot = NULL, *form = NULL;
    if (!read_checked(engine + PLAYLIST_FORM_GLOBAL, &form_slot, sizeof(form_slot)) ||
        !read_checked(form_slot, &form, sizeof(form)) || !form) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its Playlist form");
        return 0;
    }
    int32_t current = 0;
    read_checked(engine + CURRENT_ARRANGEMENT, &current, sizeof(current));
    void (*repaint)(void *) = (void (*)(void *))(engine + REPAINT_PLAYLIST);
    repaint(form);
    void (*refresh)(int) = (void (*)(int))(engine + SET_CURRENT_ARRANGEMENT);
    refresh(current);
    if (!notify_project_change(engine)) {
        return 0;
    }
    return 1;
}

PyObject *arrangement_add(PyObject *self, PyObject *args) {
    int switch_to, commit;
    const char *name;
    if (!PyArg_ParseTuple(args, "isi:add_arrangement", &switch_to, &name, &commit)) {
        return NULL;
    }
    if ((switch_to != 0 && switch_to != 1) || (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid arrangement request");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    unsigned char *data = NULL;
    long long count = 0;
    if (!arrangements_array(engine, &data, &count)) {
        return NULL;
    }
    if (!commit) {
        return PyLong_FromLong(count + 1);
    }
    void (*add_arrangement)(unsigned char, unsigned char) =
        (void (*)(unsigned char, unsigned char))(engine + ARRANGEMENT_ADD);
    add_arrangement((unsigned char)(switch_to ? 1 : 0), 0);
    long long after = 0;
    if (!arrangements_array(engine, &data, &after) || after != count + 1) {
        PyErr_SetString(PyExc_RuntimeError, "The arrangement was not added");
        return NULL;
    }
    int new_index = (int)count;
    if (name[0]) {
        void *string = NULL;
        ((void (*)(void **, const char *))(engine + FROM_UTF8))(&string, name);
        if (!string) {
            PyErr_SetString(PyExc_ValueError, "Invalid arrangement name");
            return NULL;
        }
        void (*set_name)(int, void *) = (void (*)(int, void *))(engine + ARRANGEMENT_SET_NAME);
        set_name(new_index, string);
    }
    if (!arrangement_refresh(engine)) {
        return NULL;
    }
    unsigned char *arrangement = NULL;
    wchar_t buffer[MAX_NAME_CHARS];
    if (!read_checked(data + (size_t)new_index * 8, &arrangement, sizeof(arrangement)) ||
        !arrangement) {
        PyErr_SetString(PyExc_RuntimeError, "The new arrangement is unreadable");
        return NULL;
    }
    size_t chars = 0;
    unsigned char name_bytes[MAX_NAME_CHARS * 2];
    size_t out_size = 0;
    if (read_arrangement_name(arrangement, buffer, &chars)) {
        out_size = chars * 2;
        memcpy(name_bytes, buffer, out_size);
    }
    return PyBytes_FromStringAndSize((const char *)name_bytes, (Py_ssize_t)out_size);
}

PyObject *arrangement_clone(PyObject *self, PyObject *args) {
    int source, switch_to, commit;
    const char *name;
    if (!PyArg_ParseTuple(args, "iisi:clone_arrangement", &source, &switch_to,
                          &name, &commit)) {
        return NULL;
    }
    if (source < 0 || (switch_to != 0 && switch_to != 1) ||
        (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid arrangement clone request");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    unsigned char *data = NULL;
    long long count = 0;
    if (!arrangements_array(engine, &data, &count)) {
        return NULL;
    }
    if (source >= count) {
        PyErr_SetString(PyExc_ValueError, "The source arrangement does not exist");
        return NULL;
    }
    if (!commit) {
        return PyLong_FromLong(count + 1);
    }
    void (*add_arrangement)(unsigned char, unsigned char) =
        (void (*)(unsigned char, unsigned char))(engine + ARRANGEMENT_ADD);
    add_arrangement(0, 0);
    long long after = 0;
    if (!arrangements_array(engine, &data, &after) || after != count + 1) {
        PyErr_SetString(PyExc_RuntimeError, "The arrangement was not added");
        return NULL;
    }
    int new_index = (int)count;
    void (*copy_into)(int, int) = (void (*)(int, int))(engine + ARRANGEMENT_COPY_INTO);
    copy_into(source, new_index);
    if (name[0]) {
        void *string = NULL;
        ((void (*)(void **, const char *))(engine + FROM_UTF8))(&string, name);
        if (!string) {
            PyErr_SetString(PyExc_ValueError, "Invalid arrangement name");
            return NULL;
        }
        void (*set_name)(int, void *) = (void (*)(int, void *))(engine + ARRANGEMENT_SET_NAME);
        set_name(new_index, string);
    }
    if (switch_to) {
        void (*set_current)(int) = (void (*)(int))(engine + SET_CURRENT_ARRANGEMENT);
        set_current(new_index);
    }
    if (!arrangement_refresh(engine)) {
        return NULL;
    }
    return PyLong_FromLong(new_index);
}

PyObject *arrangement_delete(PyObject *self, PyObject *args) {
    int index, commit;
    if (!PyArg_ParseTuple(args, "ii:delete_arrangement", &index, &commit)) {
        return NULL;
    }
    if (index < 0 || (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid arrangement deletion");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    unsigned char *data = NULL;
    long long count = 0;
    if (!arrangements_array(engine, &data, &count)) {
        return NULL;
    }
    if (count <= 1 || index >= count) {
        PyErr_SetString(PyExc_ValueError,
                        "The last remaining arrangement cannot be deleted");
        return NULL;
    }
    if (!commit) {
        return PyLong_FromLong(count - 1);
    }
    /* The engine delete triggers FL's autosave hook on the main thread; FL
       skips it while the load-in-progress flag is set. Mirror the reference
       implementation's verified suppression and restore the flag afterwards. */
    void *flag_slot = NULL, *flag_ptr = NULL;
    unsigned char saved_flag = 0;
    int have_flag = read_checked(engine + LOAD_IN_PROGRESS_SLOT, &flag_slot,
                                 sizeof(flag_slot)) && flag_slot &&
                    read_checked(flag_slot, &flag_ptr, sizeof(flag_ptr)) && flag_ptr &&
                    read_checked(flag_ptr, &saved_flag, sizeof(saved_flag));
    if (have_flag) {
        unsigned char one = 1;
        memcpy(flag_ptr, &one, sizeof(one));
    }
    void (*delete_arrangement)(int, unsigned char, unsigned char) =
        (void (*)(int, unsigned char, unsigned char))(engine + ARRANGEMENT_DELETE);
    delete_arrangement(index, 1, 1);
    if (have_flag) {
        memcpy(flag_ptr, &saved_flag, sizeof(saved_flag));
    }
    if (!arrangement_refresh(engine)) {
        return NULL;
    }
    long long after = 0;
    if (!arrangements_array(engine, &data, &after) || after != count - 1) {
        PyErr_SetString(PyExc_RuntimeError, "The arrangement deletion was not confirmed");
        return NULL;
    }
    return PyLong_FromLong(after);
}

PyObject *arrangement_set_name(PyObject *self, PyObject *args) {
    int index, commit;
    const char *name;
    if (!PyArg_ParseTuple(args, "isi:set_arrangement_name", &index, &name, &commit)) {
        return NULL;
    }
    if (index < 0 || (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid arrangement rename");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    unsigned char *data = NULL;
    long long count = 0;
    if (!arrangements_array(engine, &data, &count)) {
        return NULL;
    }
    if (index >= count) {
        PyErr_SetString(PyExc_ValueError, "The arrangement does not exist");
        return NULL;
    }
    if (!commit) {
        return PyBytes_FromStringAndSize("", 0);
    }
    void *string = NULL;
    ((void (*)(void **, const char *))(engine + FROM_UTF8))(&string, name);
    if (!string) {
        PyErr_SetString(PyExc_ValueError, "Invalid arrangement name");
        return NULL;
    }
    void (*set_name)(int, void *) = (void (*)(int, void *))(engine + ARRANGEMENT_SET_NAME);
    set_name(index, string);
    if (!arrangement_refresh(engine)) {
        return NULL;
    }
    unsigned char *arrangement = NULL;
    wchar_t buffer[MAX_NAME_CHARS];
    size_t chars = 0;
    if (!read_checked(data + (size_t)index * 8, &arrangement, sizeof(arrangement)) ||
        !arrangement || !read_arrangement_name(arrangement, buffer, &chars)) {
        PyErr_SetString(PyExc_RuntimeError, "The arrangement rename was not confirmed");
        return NULL;
    }
    return PyBytes_FromStringAndSize((const char *)buffer,
                                     (Py_ssize_t)(chars * 2));
}

/* Switch the visible arrangement (no content change). Returns the new index so
   the caller can confirm the switch actually happened. */
PyObject *arrangement_set_current(PyObject *self, PyObject *args) {
    int index;
    if (!PyArg_ParseTuple(args, "i:set_current", &index)) return NULL;
    if (index < 0) {
        PyErr_SetString(PyExc_ValueError, "Invalid arrangement index");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) return NULL;
    unsigned char *data = NULL;
    long long count = 0;
    if (!arrangements_array(engine, &data, &count)) return NULL;
    if (index >= count) {
        PyErr_SetString(PyExc_ValueError, "The arrangement does not exist");
        return NULL;
    }
    void (*refresh)(int) = (void (*)(int))(engine + SET_CURRENT_ARRANGEMENT);
    refresh(index);
    int32_t observed = -1;
    if (!read_checked(engine + CURRENT_ARRANGEMENT, &observed, sizeof(observed))) {
        PyErr_SetString(PyExc_RuntimeError, "Could not read back the current arrangement");
        return NULL;
    }
    return PyLong_FromLong(observed);
}

/* ---- Project save-copy ----
   The engine's FLP writer worker (called by FL's Save command wrapper whose
   error path pops modal dialogs). Writes the live project to an arbitrary
   absolute path without touching the current project state. */

PyObject *project_save_copy(PyObject *self, PyObject *args) {
    const char *path;
    if (!PyArg_ParseTuple(args, "s:save_copy", &path)) {
        return NULL;
    }
    if (strlen(path) < 5 || strlen(path) > 500 || path[0] == 0) {
        PyErr_SetString(PyExc_ValueError, "Invalid save path");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    void *string = NULL;
    ((void (*)(void **, const char *))(engine + FROM_UTF8))(&string, path);
    if (!string) {
        PyErr_SetString(PyExc_ValueError, "Invalid save path encoding");
        return NULL;
    }
    int code = ((int (*)(void *, int, void *, unsigned char))(engine + PROJECT_SAVE_WORKER))(
        string, 0, NULL, 0);
    ((void (*)(void **))(engine + FREE_STRING))(&string);
    return PyLong_FromLong(code);
}
