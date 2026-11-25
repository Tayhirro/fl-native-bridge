#include "native.h"
#include "profiles/fl_26_1_7_5654.h"
#include <math.h>
#include <string.h>

enum {
    MAX_POINTS = 4000,
    MAX_PAGE_POINTS = 64,
    STORED_POINT_SIZE = 0x20,
    POINT_PREFIX_SIZE = 0x20,
};

typedef void *(*CreateAutomation)(unsigned target, unsigned char flag, void *unused,
                                  unsigned char mode, unsigned char create);

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

static int writable_checked(const void *address, size_t size) {
    MEMORY_BASIC_INFORMATION info;
    if (!address || !VirtualQuery(address, &info, sizeof(info)) || info.State != MEM_COMMIT ||
        (info.Protect & (PAGE_NOACCESS | PAGE_GUARD)) ||
        !(info.Protect & (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE |
                          PAGE_EXECUTE_WRITECOPY))) return 0;
    uintptr_t offset = (uintptr_t)address - (uintptr_t)info.BaseAddress;
    return offset <= info.RegionSize && size <= info.RegionSize - offset;
}

PyObject *automation_read_points(PyObject *self, PyObject *args) {
    int index, offset, limit;
    if (!PyArg_ParseTuple(args, "iii:read_points", &index, &offset, &limit)) {
        return NULL;
    }
    if (index < 0 || offset < 0 || limit < 1 || limit > MAX_PAGE_POINTS) {
        PyErr_SetString(PyExc_ValueError, "Invalid automation channel or page");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    void *thread_address = NULL;
    DWORD main_thread;
    if (!engine || engine != (unsigned char *)GetModuleHandleW(L"FLEngine_x64.dll") ||
        !read_checked(engine + MAIN_THREAD, &thread_address, sizeof(thread_address)) ||
        !read_checked(thread_address, &main_thread, sizeof(main_thread)) ||
        main_thread != GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError, "Read verified automation on the FL idle thread");
        return NULL;
    }

    unsigned char *slot = NULL, *list = NULL, *items = NULL, *channel = NULL;
    unsigned char *container = NULL, *envelope = NULL, *array = NULL;
    int32_t channel_count, kind, mode;
    int64_t point_count;
    uint32_t source_event;
    double maximum;
    if (!read_checked(engine + CHANNEL_LIST, &slot, sizeof(slot)) || !slot ||
        !read_checked(slot, &list, sizeof(list)) || !list ||
        !read_checked(list + 8, &items, sizeof(items)) || !items ||
        !read_checked(list + 0x10, &channel_count, sizeof(channel_count)) ||
        channel_count < 0 || channel_count > 4096 || index >= channel_count ||
        !read_checked(items + (uintptr_t)index * sizeof(void *), &channel, sizeof(channel)) ||
        !channel || !read_checked(channel + 0x160, &kind, sizeof(kind)) || kind != 5 ||
        !read_checked(channel + 0x9c, &source_event, sizeof(source_event)) ||
        source_event >= 0x10000000 || (source_event & 0xffff) ||
        !read_checked(channel + 0x360, &container, sizeof(container)) || !container ||
        !read_checked(container + 0x10, &envelope, sizeof(envelope)) || !envelope ||
        !read_checked(envelope + 0x58, &mode, sizeof(mode)) || mode != 4 ||
        !read_checked(envelope + 0x68, &maximum, sizeof(maximum)) ||
        !isfinite(maximum) || maximum <= 0 ||
        !read_checked(envelope + 0x28, &array, sizeof(array)) || !array ||
        !read_checked(array - 8, &point_count, sizeof(point_count)) ||
        point_count < 2 || point_count > MAX_POINTS) {
        PyErr_SetString(PyExc_ValueError, "Channel is not a supported Automation Clip");
        return NULL;
    }

    int available = offset < point_count ? (int)point_count - offset : 0;
    int take = available < limit ? available : limit;
    unsigned char records[MAX_PAGE_POINTS * POINT_PREFIX_SIZE];
    double time = 0, prior_time = 0;
    for (int i = 0; take && i < offset + take; i++) {
        unsigned char raw[POINT_PREFIX_SIZE];
        float delta, value, tension;
        if (!read_checked(array + (uintptr_t)i * STORED_POINT_SIZE, raw, sizeof(raw))) {
            PyErr_SetString(PyExc_RuntimeError, "Cannot read the requested automation points");
            return NULL;
        }
        memcpy(&delta, raw, sizeof(delta));
        memcpy(&value, raw + 4, sizeof(value));
        memcpy(&tension, raw + 8, sizeof(tension));
        if (!isfinite(delta) || delta < 0 || !isfinite(value) || value < 0 || value > 1 ||
            !isfinite(tension) || tension < -1 || tension > 1 || time + delta > maximum) {
            PyErr_SetString(PyExc_RuntimeError, "Automation point values failed validation");
            return NULL;
        }
        if (i == offset) {
            prior_time = time;
        }
        time += delta;
        if (i >= offset) {
            memcpy(records + (i - offset) * POINT_PREFIX_SIZE, raw, POINT_PREFIX_SIZE);
        }
    }
    PyObject *bytes = PyBytes_FromStringAndSize((const char *)records, take * POINT_PREFIX_SIZE);
    if (!bytes) {
        return NULL;
    }
    return Py_BuildValue("(iIiddN)", index, source_event, (int)point_count,
                         maximum, prior_time, bytes);
}

/* The registry contains links independently of Playlist clip placements. */
PyObject *automation_read_links(PyObject *self, PyObject *args) {
    int channel, offset, limit;
    if (!PyArg_ParseTuple(args, "iii:read_links", &channel, &offset, &limit)) return NULL;
    if (channel < 0 || offset < 0 || limit < 1 || limit > 64) {
        PyErr_SetString(PyExc_ValueError, "Invalid automation link page");
        return NULL;
    }
    PyObject *read_args = Py_BuildValue("(iii)", channel, 0, 1);
    if (!read_args) return NULL;
    PyObject *page = automation_read_points(self, read_args);
    Py_DECREF(read_args);
    if (!page) return NULL;
    unsigned long source = PyLong_AsUnsignedLong(PyTuple_GetItem(page, 1));
    Py_DECREF(page);
    if (PyErr_Occurred()) return NULL;
    ModuleState *state = PyModule_GetState(self);
    unsigned char *root = NULL, *list = NULL, *items = NULL;
    int32_t count, capacity;
    if (!read_checked(state->engine + AUTOMATION_LINK_REGISTRY, &root, sizeof(root)) || !root ||
        !read_checked(root + 8, &list, sizeof(list)) || !list ||
        !read_checked(list + 8, &items, sizeof(items)) ||
        !read_checked(list + 0x10, &count, sizeof(count)) ||
        !read_checked(list + 0x14, &capacity, sizeof(capacity)) ||
        count < 0 || count > 8192 || capacity < count || capacity > 65536 || (count && !items)) {
        PyErr_SetString(PyExc_RuntimeError, "Automation link registry unavailable or unsupported");
        return NULL;
    }
    uint32_t records[64];
    int matches = 0, taken = 0;
    for (int i = 0; i < count; i++) {
        unsigned char *entry = NULL;
        uint32_t entry_source, target;
        if (!read_checked(items + (uintptr_t)i * sizeof(void *), &entry, sizeof(entry)) || !entry ||
            !read_checked(entry + 8, &entry_source, sizeof(entry_source)) ||
            !read_checked(entry + 0x10, &target, sizeof(target))) {
            PyErr_SetString(PyExc_RuntimeError, "Automation link registry changed or has invalid entries");
            return NULL;
        }
        if (entry_source != source) continue;
        if (matches >= offset && taken < limit) records[taken++] = target;
        matches++;
    }
    PyObject *bytes = PyBytes_FromStringAndSize((const char *)records, taken * sizeof(uint32_t));
    if (!bytes) return NULL;
    return Py_BuildValue("(iIiN)", channel, (unsigned int)source, matches, bytes);
}

/* The snapshot path at preferred VA 0x1017ea0 records the specific channel.
   A generic saveUndo(UF_AutoClip) alone does not capture the point data. */
/* field: 0 value, 1 tension, 2 absolute position. */
static PyObject *automation_set_point_float(PyObject *self, PyObject *args, int field) {
    int channel_index, point_index, commit;
    unsigned int expected_source;
    const char *expected;
    const char *expected_next = NULL;
    Py_ssize_t expected_size, expected_next_size = 0;
    double expected_prior, requested;
    int parsed = field == 2
        ? PyArg_ParseTuple(args, "iiIdy#y#di:move_point", &channel_index, &point_index,
                           &expected_source, &expected_prior, &expected, &expected_size,
                           &expected_next, &expected_next_size, &requested, &commit)
        : PyArg_ParseTuple(args, "iiIdy#di:set_point_value", &channel_index, &point_index,
                           &expected_source, &expected_prior, &expected, &expected_size,
                           &requested, &commit);
    if (!parsed) {
        return NULL;
    }
    if (channel_index < 0 || point_index < 0 || expected_size != STORED_POINT_SIZE ||
        !isfinite(expected_prior) || expected_prior < 0 || !isfinite(requested) ||
        requested < (field == 1 ? -1 : 0) || (field != 2 && requested > 1) ||
        (field == 2 && expected_next_size != 0 && expected_next_size != STORED_POINT_SIZE) ||
        (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid automation point edit");
        return NULL;
    }
    PyObject *read_args = Py_BuildValue("(iii)", channel_index, point_index, field == 2 ? 2 : 1);
    if (!read_args) return NULL;
    PyObject *page = automation_read_points(self, read_args);
    Py_DECREF(read_args);
    if (!page) return NULL;
    unsigned long source = PyLong_AsUnsignedLong(PyTuple_GetItem(page, 1));
    double prior = PyFloat_AsDouble(PyTuple_GetItem(page, 4));
    double maximum = PyFloat_AsDouble(PyTuple_GetItem(page, 3));
    long count = PyLong_AsLong(PyTuple_GetItem(page, 2));
    char *record = NULL;
    Py_ssize_t record_size = 0;
    int ok = !PyErr_Occurred() &&
        PyBytes_AsStringAndSize(PyTuple_GetItem(page, 5), &record, &record_size) == 0 &&
        source == expected_source && prior == expected_prior &&
        record_size == STORED_POINT_SIZE + (field == 2 ? expected_next_size : 0) &&
        !memcmp(record, expected, STORED_POINT_SIZE) &&
        (field != 2 || !expected_next_size ||
         !memcmp(record + STORED_POINT_SIZE, expected_next, STORED_POINT_SIZE));
    Py_DECREF(page);
    if (!ok) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "Automation point changed; read it again");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    unsigned char *slot=NULL, *list=NULL, *items=NULL, *channel=NULL, *container=NULL;
    unsigned char *envelope=NULL, *array=NULL;
    void *vtable=NULL, *recompute=NULL, *global_manager=NULL, *manager=NULL;
    int32_t *changed=NULL, changed_value;
    if (!read_checked(engine + CHANNEL_LIST, &slot, sizeof(slot)) || !slot ||
        !read_checked(slot, &list, sizeof(list)) || !list ||
        !read_checked(list + 8, &items, sizeof(items)) || !items ||
        !read_checked(items + (uintptr_t)channel_index * sizeof(void *), &channel, sizeof(channel)) || !channel ||
        !read_checked(channel + 0x360, &container, sizeof(container)) || !container ||
        !read_checked(container + 0x10, &envelope, sizeof(envelope)) || !envelope ||
        !read_checked(envelope + 0x28, &array, sizeof(array)) || !array ||
        !read_checked(envelope, &vtable, sizeof(vtable)) || !vtable ||
        !read_checked((unsigned char *)vtable + 0x40, &recompute, sizeof(recompute)) ||
        (uintptr_t)recompute < (uintptr_t)engine + TEXT_START ||
        (uintptr_t)recompute >= (uintptr_t)engine + TEXT_END ||
        !read_checked(engine + PROJECT_MANAGER, &global_manager, sizeof(global_manager)) || !global_manager ||
        !read_checked(global_manager, &manager, sizeof(manager)) || !manager ||
        !read_checked(engine + PROJECT_CHANGED_FLAG, &changed, sizeof(changed)) ||
        !read_checked(changed, &changed_value, sizeof(changed_value))) {
        PyErr_SetString(PyExc_RuntimeError, "Automation edit context failed validation");
        return NULL;
    }
    unsigned char *point = array + (uintptr_t)point_index * STORED_POINT_SIZE;
    for (long i = 0; i < count; i++) {
        unsigned char candidate[STORED_POINT_SIZE];
        if (i == point_index) continue;
        if (!read_checked(array + (uintptr_t)i * STORED_POINT_SIZE, candidate, sizeof(candidate))) {
            PyErr_SetString(PyExc_RuntimeError, "Cannot validate automation point identity");
            return NULL;
        }
        if (!memcmp(candidate, expected, sizeof(candidate))) {
            PyErr_SetString(PyExc_ValueError, "Ambiguous automation snapshot: identical points exist");
            return NULL;
        }
    }
    MEMORY_BASIC_INFORMATION point_info, changed_info;
    if (!VirtualQuery(point, &point_info, sizeof(point_info)) ||
        !(point_info.Protect & (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)) ||
        !VirtualQuery(changed, &changed_info, sizeof(changed_info)) ||
        !(changed_info.Protect & (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY))) {
        PyErr_SetString(PyExc_RuntimeError, "Automation data is not writable");
        return NULL;
    }
    float value = (float)requested, old_value, next_value = 0;
    size_t value_offset = field == 2 ? 0 : field == 1 ? 8 : 4;
    memcpy(&old_value, expected + value_offset, sizeof(old_value));
    if (field == 2) {
        if ((point_index + 1 < count) != (expected_next_size == STORED_POINT_SIZE) ||
            (point_index == 0 && requested != 0) || requested < prior || requested > maximum) {
            PyErr_SetString(PyExc_ValueError, "Position is outside the verified point interval");
            return NULL;
        }
        value = (float)(requested - prior);
        if (!isfinite(value) || prior + (double)value != requested) {
            PyErr_SetString(PyExc_ValueError, "Position is not exactly representable by the host");
            return NULL;
        }
        if (expected_next_size) {
            float old_next;
            memcpy(&old_next, expected_next, sizeof(old_next));
            double following = prior + (double)old_value + (double)old_next;
            next_value = (float)(following - requested);
            if (!isfinite(next_value) || next_value < 0 || requested + (double)next_value != following) {
                PyErr_SetString(PyExc_ValueError, "Position would move or cross the next point");
                return NULL;
            }
            MEMORY_BASIC_INFORMATION next_info;
            if (!VirtualQuery(point + STORED_POINT_SIZE, &next_info, sizeof(next_info)) ||
                !(next_info.Protect & (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE |
                                       PAGE_EXECUTE_WRITECOPY))) {
                PyErr_SetString(PyExc_RuntimeError, "Next automation point is not writable");
                return NULL;
            }
        }
    }
    if (!commit || value == old_value) return PyLong_FromLong(0);

    void *label = NULL;
    void *(*make_string)(void **, const char *) = (void *(*)(void **, const char *))(engine + FROM_UTF8);
    void *(*free_string)(void **) = (void *(*)(void **))(engine + FREE_STRING);
    void (*prepare)(void *, void *, int) =
        (void (*)(void *, void *, int))(engine + PREPARE_AUTOMATION_UNDO);
    make_string(&label, "Edit automation point");
    if (!label) {
        PyErr_SetString(PyExc_RuntimeError, "Could not construct the undo label");
        return NULL;
    }
    prepare(manager, label, channel_index);
    free_string(&label);
    memcpy(point + value_offset, &value, sizeof(value));
    if (field == 2 && expected_next_size) {
        memcpy(point + STORED_POINT_SIZE, &next_value, sizeof(next_value));
    }
    ((void (*)(void *))recompute)(envelope);
    if (changed_value != 1) {
        int32_t dirty = 1;
        memcpy(changed, &dirty, sizeof(dirty));
        ((void (*)(int, int))(engine + NOTIFY_PROJECT_CHANGE))(7, 0);
    }
    ((void (*)(void))(engine + UPDATE_PROJECT_CHANGE))();
    return PyLong_FromLong(1);
}

PyObject *automation_set_point_value(PyObject *self, PyObject *args) {
    return automation_set_point_float(self, args, 0);
}

PyObject *automation_set_point_tension(PyObject *self, PyObject *args) {
    return automation_set_point_float(self, args, 1);
}

PyObject *automation_move_point(PyObject *self, PyObject *args) {
    return automation_set_point_float(self, args, 2);
}

/* Write one point's curve-shape dword (record +0x0C..+0x0F). The FLP format
   serialises the same four bytes as one dword after the tension float (pyflp
   names them "linked to tension"), so they are written and verified as a
   unit. The host recomputes its cached segment data from the record. */
PyObject *automation_set_point_curve(PyObject *self, PyObject *args) {
    int channel_index, point_index, commit;
    unsigned int expected_source;
    const char *expected;
    Py_ssize_t expected_size;
    double expected_prior;
    unsigned int curve;
    if (!PyArg_ParseTuple(args, "iiIdy#Ii:set_point_curve", &channel_index, &point_index,
                          &expected_source, &expected_prior, &expected, &expected_size,
                          &curve, &commit)) {
        return NULL;
    }
    if (channel_index < 0 || point_index < 0 || expected_size != STORED_POINT_SIZE ||
        !isfinite(expected_prior) || expected_prior < 0 || curve > 0x7FFFFFFFu ||
        (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid automation curve edit");
        return NULL;
    }
    PyObject *read_args = Py_BuildValue("(iii)", channel_index, point_index, 1);
    if (!read_args) return NULL;
    PyObject *page = automation_read_points(self, read_args);
    Py_DECREF(read_args);
    if (!page) return NULL;
    unsigned long source = PyLong_AsUnsignedLong(PyTuple_GetItem(page, 1));
    double prior = PyFloat_AsDouble(PyTuple_GetItem(page, 4));
    long count = PyLong_AsLong(PyTuple_GetItem(page, 2));
    char *record = NULL;
    Py_ssize_t record_size = 0;
    int ok = !PyErr_Occurred() &&
        PyBytes_AsStringAndSize(PyTuple_GetItem(page, 5), &record, &record_size) == 0 &&
        source == expected_source && prior == expected_prior &&
        record_size == STORED_POINT_SIZE && !memcmp(record, expected, STORED_POINT_SIZE);
    Py_DECREF(page);
    if (!ok) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "Automation point changed; read it again");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    unsigned char *slot=NULL, *list=NULL, *items=NULL, *channel=NULL, *container=NULL;
    unsigned char *envelope=NULL, *array=NULL;
    void *vtable=NULL, *recompute=NULL, *global_manager=NULL, *manager=NULL;
    int32_t *changed=NULL, changed_value;
    if (!read_checked(engine + CHANNEL_LIST, &slot, sizeof(slot)) || !slot ||
        !read_checked(slot, &list, sizeof(list)) || !list ||
        !read_checked(list + 8, &items, sizeof(items)) || !items ||
        !read_checked(items + (uintptr_t)channel_index * sizeof(void *), &channel, sizeof(channel)) || !channel ||
        !read_checked(channel + 0x360, &container, sizeof(container)) || !container ||
        !read_checked(container + 0x10, &envelope, sizeof(envelope)) || !envelope ||
        !read_checked(envelope + 0x28, &array, sizeof(array)) || !array ||
        !read_checked(envelope, &vtable, sizeof(vtable)) || !vtable ||
        !read_checked((unsigned char *)vtable + 0x40, &recompute, sizeof(recompute)) ||
        (uintptr_t)recompute < (uintptr_t)engine + TEXT_START ||
        (uintptr_t)recompute >= (uintptr_t)engine + TEXT_END ||
        !read_checked(engine + PROJECT_MANAGER, &global_manager, sizeof(global_manager)) || !global_manager ||
        !read_checked(global_manager, &manager, sizeof(manager)) || !manager ||
        !read_checked(engine + PROJECT_CHANGED_FLAG, &changed, sizeof(changed)) ||
        !read_checked(changed, &changed_value, sizeof(changed_value))) {
        PyErr_SetString(PyExc_RuntimeError, "Automation curve edit context failed validation");
        return NULL;
    }
    unsigned char *point = array + (uintptr_t)point_index * STORED_POINT_SIZE;
    for (long i = 0; i < count; i++) {
        unsigned char candidate[STORED_POINT_SIZE];
        if (i == point_index) continue;
        if (!read_checked(array + (uintptr_t)i * STORED_POINT_SIZE, candidate, sizeof(candidate))) {
            PyErr_SetString(PyExc_RuntimeError, "Cannot validate automation point identity");
            return NULL;
        }
        if (!memcmp(candidate, expected, sizeof(candidate))) {
            PyErr_SetString(PyExc_ValueError, "Ambiguous automation snapshot: identical points exist");
            return NULL;
        }
    }
    if (!commit || !memcmp(expected + 12, &curve, sizeof(curve))) return PyLong_FromLong(0);
    if (!writable_checked(point, STORED_POINT_SIZE) ||
        !writable_checked(changed, sizeof(*changed))) {
        PyErr_SetString(PyExc_RuntimeError, "Automation data is not writable");
        return NULL;
    }
    void *label = NULL;
    void *(*make_string)(void **, const char *) = (void *(*)(void **, const char *))(engine + FROM_UTF8);
    void *(*free_string)(void **) = (void *(*)(void **))(engine + FREE_STRING);
    make_string(&label, "Edit automation point curve");
    if (!label) {
        PyErr_SetString(PyExc_RuntimeError, "Could not construct the undo label");
        return NULL;
    }
    ((void (*)(void *, void *, int))(engine + PREPARE_AUTOMATION_UNDO))(manager, label, channel_index);
    free_string(&label);
    memcpy(point + 12, &curve, sizeof(curve));
    ((void (*)(void *))recompute)(envelope);
    unsigned char observed_flags[4];
    if (!read_checked(point + 12, observed_flags, sizeof(observed_flags)) ||
        memcmp(observed_flags, &curve, sizeof(curve))) {
        PyErr_SetString(PyExc_RuntimeError, "The curve dword write was not confirmed");
        return NULL;
    }
    if (changed_value != 1) {
        int32_t dirty = 1;
        memcpy(changed, &dirty, sizeof(dirty));
        ((void (*)(int, int))(engine + NOTIFY_PROJECT_CHANGE))(7, 0);
    }
    ((void (*)(void))(engine + UPDATE_PROJECT_CHANGE))();
    return PyLong_FromLong(1);
}

/* FL's own create-automation routine appends one automation channel whose
   control event links to the requested target event. The isolated
   twin-automation fixture verified the call form (target, 0, NULL, 0, 1). */
PyObject *automation_create_source(PyObject *self, PyObject *args) {
    unsigned target;
    int expected_count, commit;
    if (!PyArg_ParseTuple(args, "Iii:create_source", &target, &expected_count, &commit)) {
        return NULL;
    }
    if (!target || target >= 0x80000000u || expected_count < 0 || expected_count >= 4096 ||
        (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid automation source creation request");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    void *thread_address = NULL;
    DWORD thread;
    if (!engine || engine != (unsigned char *)GetModuleHandleW(L"FLEngine_x64.dll") ||
        !read_checked(engine + MAIN_THREAD, &thread_address, sizeof(thread_address)) ||
        !read_checked(thread_address, &thread, sizeof(thread)) || thread != GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError, "Run the verified automation creation on the FL idle thread");
        return NULL;
    }
    unsigned char *slot = NULL, *list = NULL;
    int count;
    void *global_manager = NULL, *manager = NULL;
    if (!read_checked(engine + CHANNEL_LIST, &slot, sizeof(slot)) || !slot ||
        !read_checked(slot, &list, sizeof(list)) || !list ||
        !read_checked(list + 0x10, &count, sizeof(count)) || count != expected_count ||
        !read_checked(engine + PROJECT_MANAGER, &global_manager, sizeof(global_manager)) ||
        !read_checked(global_manager, &manager, sizeof(manager)) || !manager) {
        PyErr_SetString(PyExc_ValueError, "Channel list or project manager changed; read again");
        return NULL;
    }
    if (!commit) return PyLong_FromLong(0);
    void *created = ((CreateAutomation)(engine + CREATE_AUTOMATION))(target, 0, NULL, 0, 1);
    if (!created) {
        PyErr_SetString(PyExc_RuntimeError,
                        "FL returned no automation channel; inspect the project before retrying");
        return NULL;
    }
    unsigned char **items = NULL, *channel = NULL;
    int after;
    int grown = read_checked(list + 0x10, &after, sizeof(after)) && after == count + 1 &&
        read_checked(list + 8, &items, sizeof(items)) && items &&
        read_checked(items + (uintptr_t)count * sizeof(void *), &channel, sizeof(channel)) &&
        channel;
    /* The routine's own returns are lazy on this build: the channel's type and
       source-event fields populate on a later host cycle, and the channel list
       itself can grow one cycle after the call (both observed in isolation).
       The caller must confirm the append and identity independently; this
       operation returns whether the append is already visible. */
    return Py_BuildValue("(ii)", count, grown ? 0 : 1);
}

/* Insert or delete an interior point after validating the complete observed
   array. Delphi owns the dynamic array; no CRT realloc or raw pointer swap. */
PyObject *automation_edit_points(PyObject *self, PyObject *args) {
    int channel_index, mode, index, commit;
    unsigned int expected_source;
    const char *expected;
    Py_ssize_t expected_size;
    double position, requested_value, requested_tension;
    if (!PyArg_ParseTuple(args, "iIy#idddii:edit_points", &channel_index, &expected_source,
                          &expected, &expected_size, &mode, &position, &requested_value,
                          &requested_tension, &index, &commit)) return NULL;
    if (channel_index < 0 || expected_size < 2 * STORED_POINT_SIZE ||
        expected_size % STORED_POINT_SIZE || expected_size > MAX_POINTS * STORED_POINT_SIZE ||
        (mode != 0 && mode != 1) || (commit != 0 && commit != 1) ||
        !isfinite(position) || !isfinite(requested_value) || !isfinite(requested_tension) ||
        position < 0 || requested_value < 0 || requested_value > 1 ||
        requested_tension < -1 || requested_tension > 1) {
        PyErr_SetString(PyExc_ValueError, "Invalid automation point array edit");
        return NULL;
    }
    int count = (int)(expected_size / STORED_POINT_SIZE);
    if ((mode == 0 && (count == MAX_POINTS || index < 1 || index >= count)) ||
        (mode == 1 && (count <= 2 || index <= 0 || index >= count - 1))) {
        PyErr_SetString(PyExc_ValueError, "Insertion must be between points; endpoints cannot be deleted");
        return NULL;
    }
    PyObject *read_args = Py_BuildValue("(iii)", channel_index, 0, 1);
    if (!read_args) return NULL;
    PyObject *page = automation_read_points(self, read_args);
    Py_DECREF(read_args);
    if (!page) return NULL;
    unsigned long source = PyLong_AsUnsignedLong(PyTuple_GetItem(page, 1));
    long observed_count = PyLong_AsLong(PyTuple_GetItem(page, 2));
    double maximum = PyFloat_AsDouble(PyTuple_GetItem(page, 3));
    int ok = !PyErr_Occurred() && source == expected_source && observed_count == count;
    Py_DECREF(page);
    if (!ok) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "Automation source changed");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine, *slot=NULL, *list=NULL, *items=NULL, *channel=NULL;
    unsigned char *container=NULL, *envelope=NULL, *array=NULL;
    void *vtable=NULL, *recompute=NULL, *global_manager=NULL, *manager=NULL, *typeinfo=NULL;
    int32_t *changed=NULL, changed_value;
    if (!read_checked(engine + CHANNEL_LIST, &slot, sizeof(slot)) || !slot ||
        !read_checked(slot, &list, sizeof(list)) || !list ||
        !read_checked(list + 8, &items, sizeof(items)) || !items ||
        !read_checked(items + (uintptr_t)channel_index * sizeof(void *), &channel, sizeof(channel)) || !channel ||
        !read_checked(channel + 0x360, &container, sizeof(container)) || !container ||
        !read_checked(container + 0x10, &envelope, sizeof(envelope)) || !envelope ||
        !read_checked(envelope + 0x28, &array, sizeof(array)) || !array ||
        !read_checked(envelope, &vtable, sizeof(vtable)) || !vtable ||
        !read_checked((unsigned char *)vtable + 0x40, &recompute, sizeof(recompute)) ||
        (uintptr_t)recompute < (uintptr_t)engine + TEXT_START ||
        (uintptr_t)recompute >= (uintptr_t)engine + TEXT_END ||
        !read_checked(engine + PROJECT_MANAGER, &global_manager, sizeof(global_manager)) || !global_manager ||
        !read_checked(global_manager, &manager, sizeof(manager)) || !manager ||
        !read_checked(engine + AUTOMATION_ARRAY_TYPE, &typeinfo, sizeof(typeinfo)) ||
        (uintptr_t)typeinfo < (uintptr_t)engine ||
        (uintptr_t)typeinfo >= (uintptr_t)engine + AUTOMATION_ARRAY_TYPE + 0x1000 ||
        !read_checked(engine + PROJECT_CHANGED_FLAG, &changed, sizeof(changed)) ||
        !read_checked(changed, &changed_value, sizeof(changed_value))) {
        PyErr_SetString(PyExc_RuntimeError, "Automation array context failed validation");
        return NULL;
    }
    if (!writable_checked(envelope + 0x28, sizeof(void *)) ||
        !writable_checked(changed, sizeof(*changed))) {
        PyErr_SetString(PyExc_RuntimeError, "Automation array state is not writable");
        return NULL;
    }
    int new_count = count + (mode == 0 ? 1 : -1);
    unsigned char *copy = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY,
                                    (SIZE_T)new_count * STORED_POINT_SIZE);
    if (!copy) return PyErr_NoMemory();
    double current = 0, previous = 0, following = 0;
    int valid = 1;
    for (int i = 0; i < count; i++) {
        unsigned char actual[STORED_POINT_SIZE];
        float delta, value, tension;
        if (!read_checked(array + (uintptr_t)i * STORED_POINT_SIZE, actual, sizeof(actual)) ||
            memcmp(actual, expected + (size_t)i * STORED_POINT_SIZE, sizeof(actual))) { valid = 0; break; }
        memcpy(&delta, actual, 4); memcpy(&value, actual + 4, 4); memcpy(&tension, actual + 8, 4);
        if (actual[12] != 0 || !isfinite(delta) || (i == 0 ? delta != 0 : delta <= 0) || !isfinite(value) ||
            value < 0 || value > 1 || !isfinite(tension) || tension < -1 || tension > 1) {
            valid = 0; break;
        }
        current += delta;
        if (current > maximum) { valid = 0; break; }
        if (i == index - 1) previous = current;
        if (i == (mode == 0 ? index : index + 1)) following = current;
        if (!(mode == 1 && i == index)) {
            int destination = mode == 0 ? i + (i >= index) : i - (i > index);
            memcpy(copy + (size_t)destination * STORED_POINT_SIZE, actual, sizeof(actual));
        }
    }
    if (valid && mode == 0) {
        float new_delta = (float)(position - previous), next_delta = (float)(following - position);
        float v = (float)requested_value, t = (float)requested_tension;
        if (!(position > previous && position < following) ||
            previous + (double)new_delta != position || position + (double)next_delta != following) valid = 0;
        else {
            unsigned char *created = copy + (size_t)index * STORED_POINT_SIZE;
            memcpy(created, &new_delta, 4); memcpy(created + 4, &v, 4); memcpy(created + 8, &t, 4);
            memcpy(created + STORED_POINT_SIZE, &next_delta, 4);
        }
    } else if (valid) {
        float next_delta = (float)(following - previous);
        if (!(following > previous) || previous + (double)next_delta != following) valid = 0;
        else memcpy(copy + (size_t)index * STORED_POINT_SIZE, &next_delta, 4);
    }
    if (!valid) {
        HeapFree(GetProcessHeap(), 0, copy);
        PyErr_SetString(PyExc_ValueError, "Curve changed, is non-linear, unordered, or time is not exact");
        return NULL;
    }
    if (!commit) { HeapFree(GetProcessHeap(), 0, copy); return PyLong_FromLong(0); }
    void *label = NULL;
    void *(*make_string)(void **, const char *) = (void *(*)(void **, const char *))(engine + FROM_UTF8);
    void *(*free_string)(void **) = (void *(*)(void **))(engine + FREE_STRING);
    make_string(&label, mode == 0 ? "Insert automation point" : "Delete automation point");
    if (!label) { HeapFree(GetProcessHeap(), 0, copy); PyErr_SetString(PyExc_RuntimeError, "Undo label unavailable"); return NULL; }
    ((void (*)(void *, void *, int))(engine + PREPARE_AUTOMATION_UNDO))(manager, label, channel_index);
    free_string(&label);
    ((void (*)(void *, void *, int, intptr_t))(engine + DELPHI_DYNARRAY_SET_LENGTH))(
        envelope + 0x28, typeinfo, 1, new_count);
    int64_t new_length = 0;
    if (!read_checked(envelope + 0x28, &array, sizeof(array)) || !array ||
        !read_checked(array - 8, &new_length, sizeof(new_length)) || new_length != new_count ||
        !writable_checked(array, (size_t)new_count * STORED_POINT_SIZE)) {
        HeapFree(GetProcessHeap(), 0, copy);
        PyErr_SetString(PyExc_RuntimeError, "Automation array resize unconfirmed");
        return NULL;
    }
    memcpy(array, copy, (size_t)new_count * STORED_POINT_SIZE);
    HeapFree(GetProcessHeap(), 0, copy);
    ((void (*)(void *))recompute)(envelope);
    if (changed_value != 1) {
        int32_t dirty = 1;
        memcpy(changed, &dirty, sizeof(dirty));
        ((void (*)(int, int))(engine + NOTIFY_PROJECT_CHANGE))(7, 0);
    }
    ((void (*)(void))(engine + UPDATE_PROJECT_CHANGE))();
    return PyLong_FromLong(1);
}

/* Read-only diagnostic: dump an Automation Clip's envelope vtable slots,
   envelope header, and the first point records. Used to identify the real
   coefficient-recompute slot for offline disassembly. Never writes memory. */
PyObject *automation_vtable_dump(PyObject *self, PyObject *args) {
    int index;
    if (!PyArg_ParseTuple(args, "i:vtable_dump", &index)) return NULL;
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    void *thread_address = NULL;
    DWORD thread;
    if (!engine || engine != (unsigned char *)GetModuleHandleW(L"FLEngine_x64.dll") ||
        !read_checked(engine + MAIN_THREAD, &thread_address, sizeof(thread_address)) ||
        !read_checked(thread_address, &thread, sizeof(thread)) || thread != GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError, "Read the automation vtable on the FL idle thread");
        return NULL;
    }
    unsigned char *slot = NULL, *list = NULL, *items = NULL, *channel = NULL;
    unsigned char *container = NULL, *envelope = NULL, *vtable = NULL, *array = NULL;
    int32_t count, mode;
    int64_t point_count;
    if (!read_checked(engine + CHANNEL_LIST, &slot, sizeof(slot)) || !slot ||
        !read_checked(slot, &list, sizeof(list)) || !list ||
        !read_checked(list + 8, &items, sizeof(items)) || !items ||
        !read_checked(list + 0x10, &count, sizeof(count)) ||
        index < 0 || index >= count ||
        !read_checked(items + (uintptr_t)index * sizeof(void *), &channel, sizeof(channel)) ||
        !channel || !read_checked(channel + 0x160, &mode, sizeof(mode)) || mode != 5 ||
        !read_checked(channel + 0x360, &container, sizeof(container)) || !container ||
        !read_checked(container + 0x10, &envelope, sizeof(envelope)) || !envelope ||
        !read_checked(envelope, &vtable, sizeof(vtable)) || !vtable ||
        !read_checked(envelope + 0x28, &array, sizeof(array)) || !array ||
        !read_checked(array - 8, &point_count, sizeof(point_count)) || point_count < 2) {
        PyErr_SetString(PyExc_ValueError, "Channel is not a supported Automation Clip");
        return NULL;
    }
    unsigned char slots[24 * sizeof(void *)];
    unsigned char body[0x70];
    unsigned char points[4 * STORED_POINT_SIZE];
    if (!read_checked(vtable, slots, sizeof(slots)) ||
        !read_checked(envelope, body, sizeof(body)) ||
        !read_checked(array, points, sizeof(points))) {
        PyErr_SetString(PyExc_RuntimeError, "Could not dump automation objects");
        return NULL;
    }
    return Py_BuildValue("(KKKNNN)", (unsigned long long)(uintptr_t)engine,
                         (unsigned long long)(uintptr_t)envelope,
                         (unsigned long long)(uintptr_t)array,
                         PyBytes_FromStringAndSize((const char *)slots, sizeof(slots)),
                         PyBytes_FromStringAndSize((const char *)body, sizeof(body)),
                         PyBytes_FromStringAndSize((const char *)points, sizeof(points)));
}
