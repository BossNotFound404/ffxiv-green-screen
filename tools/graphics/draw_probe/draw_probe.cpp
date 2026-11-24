// One-frame diagnostic for a separately configured D3D11 test host.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <bcrypt.h>
#include <d3d11.h>
#include <d3d11_4.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <reshade.hpp>
#include <algorithm>
#include <array>
#include <atomic>
#include <bitset>
#include <charconv>
#include <cstring>
#include <deque>
#include <cwchar>
#include <filesystem>
#include <iomanip>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>
#include "tower_texture_inventory.hpp"
#include "fan_texture_inventory.hpp"
#include "draw_packet_inventory.hpp"

using namespace reshade;
using Microsoft::WRL::ComPtr;
namespace {
constexpr size_t slots = D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT;
constexpr size_t max_rows = 8192;
constexpr unsigned pixel_edge_limit = 512;
constexpr unsigned legacy_texture_count = 4;
constexpr unsigned core_slots = 2, early_slots = 3, late_slots = 5, neighbor_slots = 2;
constexpr unsigned early_begin = core_slots;
constexpr unsigned late_begin = early_begin + early_slots;
constexpr unsigned neighbor_begin = late_begin + late_slots;
constexpr unsigned terminal_index = neighbor_begin + neighbor_slots;
constexpr unsigned neighbor_distance = 5;
constexpr uint64_t fan_timeout_ms = 30000;
constexpr unsigned fan_coverage_slots = 2, fan_add_slots = 2;
constexpr unsigned fan_add_begin = terminal_index - fan_add_slots;
constexpr unsigned fan_lightning_slots = fan_add_begin - fan_coverage_slots;
constexpr unsigned fan_coverage_texture = 3, fan_lightning_texture = 4, fan_add_texture = 5;
constexpr size_t fan_observation_limit = 64;
constexpr unsigned fan_sequence_max_frames = 360;
unsigned requested_sequence_frames = 0;
static_assert(fan_inventory::fingerprints[fan_coverage_texture].format == fan_inventory::storage::bc3);
static_assert(fan_inventory::fingerprints[fan_lightning_texture].format == fan_inventory::storage::bc1);
constexpr unsigned rgb_write_mask = D3D11_COLOR_WRITE_ENABLE_RED | D3D11_COLOR_WRITE_ENABLE_GREEN |
                                    D3D11_COLOR_WRITE_ENABLE_BLUE;
using tower_inventory::fingerprints;

struct matched_view { unsigned slot, texture; };
struct draw_arguments {
    bool indexed;
    uint32_t count, instances, first_element;
    int32_t base_vertex;
    uint32_t first_instance;
};
struct draw {
    draw_arguments args;
    bool reflected;
    unsigned bound_views;
    DWORD thread_id;
    D3D11_RENDER_TARGET_BLEND_DESC blend;
    std::vector<matched_view> matches;
    size_t draw_ordinal = 0;
};
struct surface_identity {
    ComPtr<ID3D11Texture2D> texture;
    ComPtr<ID3D11RenderTargetView> view;
};
struct draw_pipeline {
    std::bitset<D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT> render_targets;
    std::bitset<D3D11_1_UAV_SLOT_COUNT> output_uavs;
    std::bitset<D3D11_SO_BUFFER_SLOT_COUNT> stream_output;
    unsigned inspected_uav_slots = 0;
    bool predication = false;
    bool depth_stencil_bound = false, default_depth_stencil_state = false;
    bool depth_test = false, stencil_test = false;
    bool depth_may_write = false, stencil_may_write = false;
    DXGI_FORMAT depth_stencil_format = DXGI_FORMAT_UNKNOWN;
    UINT depth_stencil_view_flags = 0;
};
struct pixel_capture {
    size_t sequence = 0;
    const char *status = nullptr;
    const char *rule = nullptr;
    const char *layout = nullptr;
    DXGI_FORMAT resource_format = DXGI_FORMAT_UNKNOWN, view_format = DXGI_FORMAT_UNKNOWN;
    unsigned samples = 0, target_width = 0, target_height = 0;
    unsigned x = 0, y = 0, width = 0, height = 0, row_bytes = 0;
    bool srgb = false, forwarded = false, terminal = false;
    unsigned resource_id = 0, view_id = 0, back_buffer_index = 0;
    uint64_t producer_cookie = 0, next_poll = 0;
    DWORD readback_thread = 0;
    ComPtr<ID3D11Texture2D> before, after;
    surface_identity identity;
    std::optional<draw_pipeline> pipeline;
    std::string before_file, after_file;
    bool fan_expected_state = false;
    std::optional<unsigned> rgb_changed_pixels;
    bool packet_selected = false, alpha_witness = false;
    size_t witness_file_bytes = 0;
    std::string packet_status, packet_bytes, packet_file, packet_inputs;
    std::optional<dp::packet_readbacks> packet_staging;
};

struct fan_observation {
    unsigned texture, slot;
    D3D11_RENDER_TARGET_BLEND_DESC blend;
    DXGI_FORMAT view_format;
    unsigned count = 1;
};

class native_draw_scope {
public:
    native_draw_scope(ID3D11DeviceContext *context, bool enabled) {
        if (enabled && SUCCEEDED(context->QueryInterface(IID_PPV_ARGS(&threading_))) &&
            threading_->GetMultithreadProtected()) {
            threading_->Enter();
            locked_ = true;
        }
    }
    ~native_draw_scope() {
        if (locked_) threading_->Leave();
    }
    native_draw_scope(const native_draw_scope &) = delete;
    native_draw_scope &operator=(const native_draw_scope &) = delete;
    bool known_threading() const { return threading_ != nullptr; }
    bool protected_context() const { return locked_; }
private:
    ComPtr<ID3D11Multithread> threading_;
    bool locked_ = false;
};

std::mutex gate;
std::atomic<bool> recording = false, done = false;
std::atomic<bool> report_pending = false;
std::atomic<bool> rearm_sidecar = false;
std::wstring report_path;
std::wstring request_path;
std::wstring profile_report_base;
std::string request_token;
HANDLE changes = INVALID_HANDLE_VALUE;
bool manual = false;
HMODULE profile_module = nullptr;
std::once_flag profile_once;
const char *configuration_source = "environment";
const char *pixel_capture_mode = "disabled";
bool pixel_capture_enabled = false;
bool observed_ffxiv_capture = false;
bool whole_tower = false;
bool fan_enabled = false, fan_active = false;
bool fan_packet_enabled = false, fan_packet_requested = false, sequence_witness_armed = false;
bool fan_light_packets_requested = false;
std::array<size_t, 2> fan_light_packet_sequences{}; // Screen, Add
unsigned fan_packet_attempt_present = 0;
size_t fan_packet_attempt_sequence = 0;
std::string fan_packet_attempt_detail;
const char *fan_status = nullptr;
uint64_t fan_deadline = 0, fan_started = 0, fan_elapsed = 0;
unsigned fan_waited_presents = 0, fan_rejected_draws = 0, fan_budget_dropped = 0;
unsigned fan_observations_dropped = 0, fan_identified_draws = 0, fan_unreflected_draws = 0;
const char *first_fan_stage_failure = nullptr;
std::array<unsigned, 3> fan_staged_draws{};
size_t fan_coverage_sequence = 0, fan_lightning_sequence = 0;
bool fan_anchors_share_target = false;
std::vector<fan_observation> fan_observations;
bool draw_packet_enabled = false;
unsigned session_shot = 1, sampled_present = 0;
DWORD arm_present_thread = 0, sampled_present_thread = 0;
std::array<pixel_capture, terminal_index + 1> pixel_captures;
api::resource_desc sampled_back_buffer{};
uint64_t readback_deadline = 0;
bool readback_in_progress = false, readback_cancelled = false;
unsigned early_budget_dropped = 0, late_budget_dropped = 0, neighbor_budget_dropped = 0;
unsigned late_budget_borrowed = 0;
unsigned target_present = 3, presents = 0, draw_count = 0, indexed_count = 0, deferred = 0;
api::swapchain *chain = nullptr;
api::command_list *immediate = nullptr;
std::unordered_map<uint64_t, unsigned> textures;
std::unordered_map<uint64_t, unsigned> fan_textures;
std::unordered_map<uint64_t, std::bitset<slots>> shader_slots;
std::vector<draw> rows;

uint64_t thread_cookie() {
    static std::atomic<uint64_t> next{1};
    thread_local const uint64_t cookie = next.fetch_add(1, std::memory_order_relaxed);
    return cookie;
}

void finish_report(api::command_list *list, bool in_present_callback);

std::filesystem::path module_path(HMODULE module = nullptr) {
    wchar_t path[32768]{};
    DWORD n = GetModuleFileNameW(module, path, static_cast<DWORD>(std::size(path)));
    return (!n || n >= std::size(path)) ? std::filesystem::path{} : std::filesystem::path(path);
}

bool fixture_host(const std::wstring &name) {
    return _wcsicmp(name.c_str(), L"tower_draw_lab.exe") == 0 || _wcsicmp(name.c_str(), L"tower_draw_pair.exe") == 0;
}

bool test_client_host(const std::wstring &name) {
    return _wcsicmp(name.c_str(), L"ffxiv_dx11.exe") == 0;
}

void configure_pixel_capture() {
    wchar_t value[2]{};
    if (GetEnvironmentVariableW(L"GREEN_SCREEN_DRAW_PIXELS", value, static_cast<DWORD>(std::size(value))) != 1 ||
        value[0] != L'1') return;
    const auto host = module_path().filename().wstring();
    observed_ffxiv_capture = manual && test_client_host(host);
    pixel_capture_enabled = fixture_host(host) || observed_ffxiv_capture;
    pixel_capture_mode = pixel_capture_enabled ? "before_after_rt0" : "unsupported_host_or_frame_mode";
    whole_tower = pixel_capture_enabled &&
        GetEnvironmentVariableW(L"GREEN_SCREEN_DRAW_WHOLE_TOWER", value, static_cast<DWORD>(std::size(value))) == 1 &&
        value[0] == L'1';
    fan_enabled = pixel_capture_enabled && manual &&
        GetEnvironmentVariableW(L"GREEN_SCREEN_DRAW_FAN_CAST", value, static_cast<DWORD>(std::size(value))) == 1 &&
        value[0] == L'1';
    const bool capture_packet =
        GetEnvironmentVariableW(L"GREEN_SCREEN_DRAW_PACKET", value, static_cast<DWORD>(std::size(value))) == 1 &&
        value[0] == L'1';
    draw_packet_enabled = whole_tower && capture_packet;
    fan_packet_enabled = fan_enabled && capture_packet;
}

bool configure_environment(bool &allow_profile) {
    wchar_t path[32768]{};
    SetLastError(ERROR_SUCCESS);
    DWORD length = GetEnvironmentVariableW(L"GREEN_SCREEN_DRAW_REPORT", path, static_cast<DWORD>(std::size(path)));
    allow_profile = length == 0 && GetLastError() == ERROR_ENVVAR_NOT_FOUND;
    if (!length || length >= std::size(path) || !std::filesystem::path(path).is_absolute()) return false;
    report_path = path;
    char number[12]{};
    DWORD n = GetEnvironmentVariableA("GREEN_SCREEN_DRAW_FRAME", number, static_cast<DWORD>(std::size(number)));
    if (n >= std::size(number)) return false;
    if (std::strcmp(number, "manual") == 0) {
        const auto root = std::filesystem::path(path).root_name().wstring();
        if (root.size() != 2 || root[1] != L':') return false;
        manual = true;
        request_path = report_path + L".arm";
    } else if (n) {
        const auto parsed = std::from_chars(number, number + n, target_present);
        if (parsed.ec != std::errc{} || parsed.ptr != number + n || target_present < 2) return false;
    }
    configure_pixel_capture();
    return true;
}

bool local_fixed_path(const std::filesystem::path &path) {
    const auto drive = path.root_name().wstring();
    return path.is_absolute() && drive.size() == 2 && drive[1] == L':' &&
           GetDriveTypeW(path.root_path().c_str()) == DRIVE_FIXED;
}

std::filesystem::path fixed_report_directory(const std::filesystem::path &directory) {
    if (!local_fixed_path(directory)) return {};
    HANDLE file = CreateFileW(directory.c_str(), FILE_READ_ATTRIBUTES,
                              FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
                              FILE_FLAG_BACKUP_SEMANTICS, nullptr);
    if (file == INVALID_HANDLE_VALUE) return {};
    BY_HANDLE_FILE_INFORMATION info{};
    wchar_t resolved[32768]{};
    const bool is_directory = GetFileInformationByHandle(file, &info) && (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY);
    const DWORD size = is_directory ? GetFinalPathNameByHandleW(file, resolved, static_cast<DWORD>(std::size(resolved)),
                                                                FILE_NAME_NORMALIZED | VOLUME_NAME_DOS) : 0;
    CloseHandle(file);
    if (!size || size >= std::size(resolved) || std::wcsncmp(resolved, L"\\\\?\\", 4) != 0) return {};
    // Resolve the directory first so a link under a fixed drive cannot select a network share.
    const std::filesystem::path final_path(resolved + 4);
    return local_fixed_path(final_path) ? final_path : std::filesystem::path{};
}

std::wstring decode_profile(const std::filesystem::path &path) {
    constexpr size_t max_profile_bytes = 128 * 1024;
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                              FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (file == INVALID_HANDLE_VALUE) return {};
    BY_HANDLE_FILE_INFORMATION info{};
    if (GetFileType(file) != FILE_TYPE_DISK || !GetFileInformationByHandle(file, &info) || info.nFileSizeHigh != 0 ||
        info.nFileSizeLow == 0 || info.nFileSizeLow > max_profile_bytes ||
        (info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))) {
        CloseHandle(file);
        return {};
    }
    std::string bytes(info.nFileSizeLow, '\0');
    DWORD read = 0;
    const bool read_all = ReadFile(file, bytes.data(), info.nFileSizeLow, &read, nullptr) && read == info.nFileSizeLow;
    CloseHandle(file);
    if (!read_all || bytes.find('\0') != std::string::npos) return {};
    std::string_view input(bytes);
    if (input.substr(0, 3) == "\xef\xbb\xbf") input.remove_prefix(3);
    const auto length = static_cast<int>(input.size());
    const int needed = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, input.data(), length, nullptr, 0);
    if (!needed) return {};
    std::wstring text(needed, L'\0');
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, input.data(), length, text.data(), needed)) return {};
    return text;
}

std::wstring_view trim_profile(std::wstring_view text) {
    const size_t start = text.find_first_not_of(L" \t\r");
    if (start == std::wstring_view::npos) return {};
    const size_t end = text.find_last_not_of(L" \t\r");
    return text.substr(start, end - start + 1);
}

std::filesystem::path profile_directory(const std::filesystem::path &profile, bool &capture_whole_tower,
                                        bool &capture_packet, bool &capture_shader_cache, bool &capture_fan) {
    std::wistringstream input(decode_profile(profile));
    std::wstring line, directory;
    bool in_capture = false;
    bool has_whole_tower_setting = false, has_packet_setting = false;
    bool has_shader_cache_setting = false, has_fan_setting = false;
    for (; std::getline(input, line); ) {
        const auto text = trim_profile(line);
        if (text.empty() || text.front() == L';' || text.front() == L'#') continue;
        if (text.front() == L'[') {
            if (text.back() != L']') return {};
            const std::wstring section(trim_profile(text.substr(1, text.size() - 2)));
            in_capture = _wcsicmp(section.c_str(), L"capture") == 0;
            continue;
        }
        if (!in_capture) continue;
        const size_t equals = text.find(L'=');
        if (equals == std::wstring_view::npos) return {};
        const std::wstring key(trim_profile(text.substr(0, equals)));
        if (_wcsicmp(key.c_str(), L"FanCast") == 0) {
            const auto value = trim_profile(text.substr(equals + 1));
            if (has_fan_setting || (value != L"0" && value != L"1")) return {};
            has_fan_setting = true;
            capture_fan = value == L"1";
            continue;
        }
        if (_wcsicmp(key.c_str(), L"DrawPacket") == 0) {
            const auto value = trim_profile(text.substr(equals + 1));
            if (has_packet_setting || (value != L"0" && value != L"1")) return {};
            has_packet_setting = true;
            capture_packet = value == L"1";
            continue;
        }
        if (_wcsicmp(key.c_str(), L"ShaderCache") == 0) {
            const auto value = trim_profile(text.substr(equals + 1));
            if (has_shader_cache_setting || (value != L"0" && value != L"1")) return {};
            has_shader_cache_setting = true;
            capture_shader_cache = value == L"1";
            continue;
        }
        if (_wcsicmp(key.c_str(), L"WholeTower") == 0) {
            const auto value = trim_profile(text.substr(equals + 1));
            if (has_whole_tower_setting || (value != L"0" && value != L"1")) return {};
            has_whole_tower_setting = true;
            capture_whole_tower = value == L"1";
            continue;
        }
        if (_wcsicmp(key.c_str(), L"ReportDirectory") != 0) continue;
        if (!directory.empty()) return {};
        directory = trim_profile(text.substr(equals + 1));
        if (directory.empty()) return {};
    }
    return fixed_report_directory(directory);
}

void init_profile(api::device *device) {
    if (device->get_api() != api::device_api::d3d11) return;
    std::call_once(profile_once, [] {
        const auto parent = module_path(profile_module).parent_path();
        if (!local_fixed_path(parent)) return;
        const auto profile = parent / L"GreenScreenDrawProbe.lab.ini";
        if (GetFileAttributesW(profile.c_str()) == INVALID_FILE_ATTRIBUTES) return;
        bool capture_whole_tower = false, capture_packet = false, capture_shader_cache = false;
        bool capture_fan = false;
        const auto directory = profile_directory(profile, capture_whole_tower, capture_packet, capture_shader_cache,
                                                 capture_fan);
        if (directory.empty()) {
            log::message(log::level::warning, "Draw probe lab profile needs a valid UTF-8 file and an existing local fixed-drive report directory.");
            return;
        }
        FILETIME started{}, exited{}, kernel{}, user{};
        if (!GetProcessTimes(GetCurrentProcess(), &started, &exited, &kernel, &user)) GetSystemTimeAsFileTime(&started);
        const auto stamp = (uint64_t(started.dwHighDateTime) << 32) | started.dwLowDateTime;
        const auto filename = L"draws-" + std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(stamp);
        profile_report_base = (directory / filename).wstring();
        report_path = profile_report_base + L".json";
        request_path = report_path + L".arm";
        manual = true;
        configuration_source = "sidecar";
        pixel_capture_enabled = true;
        observed_ffxiv_capture = test_client_host(module_path().filename().wstring());
        pixel_capture_mode = "before_after_rt0";
        whole_tower = capture_whole_tower;
        fan_enabled = capture_fan;
        draw_packet_enabled = whole_tower && capture_packet;
        fan_packet_enabled = fan_enabled && capture_packet;
        if ((draw_packet_enabled || fan_packet_enabled) && capture_shader_cache)
            dp::open_shader_cache(profile_report_base + L".shader-cache.tmp");
        rearm_sidecar.store(whole_tower || fan_enabled);
        done.store(false);
    });
}

void init_texture(api::device *device, const api::resource_desc &desc, const api::subresource_data *data,
                  api::resource_usage, api::resource resource) {
    if ((done.load() && !rearm_sidecar.load()) || !data || !data->data || device->get_api() != api::device_api::d3d11 ||
        desc.type != api::resource_type::texture_2d) return;
    const auto &tex = desc.texture;
    if (tex.depth_or_layers != 1 || tex.samples != 1) return;
    const bool bc1 = tex.format == api::format::bc1_unorm || tex.format == api::format::bc1_unorm_srgb ||
                     tex.format == api::format::bc1_typeless;
    const bool bc3 = tex.format == api::format::bc3_unorm || tex.format == api::format::bc3_unorm_srgb ||
                     tex.format == api::format::bc3_typeless;
    const bool a8 = tex.format == api::format::a8_unorm;
    if (!bc1 && !((whole_tower || fan_enabled) && a8) && !(fan_enabled && bc3)) return;
    const unsigned texture_count = whole_tower ? static_cast<unsigned>(std::size(fingerprints)) : legacy_texture_count;
    const auto tower_description = [&](const tower_inventory::texture &fp) {
        const bool format_matches = (bc1 && fp.format == tower_inventory::storage::bc1) ||
                                    (a8 && whole_tower && fp.format == tower_inventory::storage::a8);
        return format_matches && fp.width == tex.width && fp.height == tex.height;
    };
    const auto fan_description = [&](const fan_inventory::texture &fp) {
        const bool format_matches = (bc1 && fp.format == fan_inventory::storage::bc1) ||
                                    (bc3 && fp.format == fan_inventory::storage::bc3) ||
                                    (a8 && fp.format == fan_inventory::storage::a8);
        return format_matches && fp.width == tex.width && fp.height == tex.height;
    };
    const bool tower_candidate = std::any_of(fingerprints, fingerprints + texture_count, tower_description);
    const bool fan_candidate = fan_enabled &&
        std::any_of(std::begin(fan_inventory::fingerprints), std::end(fan_inventory::fingerprints), fan_description);
    if (!tower_candidate && !fan_candidate) return;

    // A creation-time digest remains a valid identity for immutable resources only.
    auto native = reinterpret_cast<ID3D11Resource *>(resource.handle);
    ComPtr<ID3D11Texture2D> texture;
    if (FAILED(native->QueryInterface(IID_PPV_ARGS(&texture)))) return;
    D3D11_TEXTURE2D_DESC actual{};
    texture->GetDesc(&actual);
    if (actual.Usage != D3D11_USAGE_IMMUTABLE) return;
    const unsigned stride = a8 ? tex.width : ((tex.width + 3) / 4) * (bc1 ? 8 : 16);
    const unsigned height = a8 ? tex.height : (tex.height + 3) / 4;
    if (data->row_pitch < stride) return;
    std::vector<UCHAR> blocks(stride * height);
    const auto *base = static_cast<const UCHAR *>(data->data);
    for (unsigned y = 0; y < height; ++y)
        std::memcpy(blocks.data() + y * stride, base + size_t(y) * data->row_pitch, stride);
    UCHAR digest[32]{};
    if (BCryptHash(BCRYPT_SHA256_ALG_HANDLE, nullptr, 0, blocks.data(),
                   static_cast<ULONG>(blocks.size()), digest, sizeof(digest)) < 0) return;
    constexpr char hex[] = "0123456789abcdef";
    char text[65]{};
    for (unsigned i = 0; i < sizeof(digest); ++i) {
        text[i * 2] = hex[digest[i] >> 4];
        text[i * 2 + 1] = hex[digest[i] & 15];
    }
    const auto find_index = [&](const auto *begin, const auto *end, const auto &description) {
        const auto match = std::find_if(begin, end, [&](const auto &fp) {
            return description(fp) && fp.mip0_bytes == blocks.size() && std::strcmp(text, fp.hash) == 0;
        });
        return match == end ? -1 : static_cast<int>(match - begin);
    };
    const auto tower_index = tower_candidate ? find_index(fingerprints, fingerprints + texture_count, tower_description) : -1;
    const auto fan_index = fan_candidate ? find_index(std::begin(fan_inventory::fingerprints),
                                                      std::end(fan_inventory::fingerprints), fan_description) : -1;
    if (tower_index >= 0 || fan_index >= 0) {
        std::lock_guard lock(gate);
        if (tower_index >= 0) textures[resource.handle] = static_cast<unsigned>(tower_index);
        if (fan_index >= 0) fan_textures[resource.handle] = static_cast<unsigned>(fan_index);
    }
}

void init_pipeline(api::device *device, api::pipeline_layout, uint32_t count,
                   const api::pipeline_subobject *parts, api::pipeline pipeline) {
    if ((done.load() && !rearm_sidecar.load()) || device->get_api() != api::device_api::d3d11) return;
    if (draw_packet_enabled || fan_packet_enabled) dp::remember(device, parts, count, pipeline);
    for (uint32_t i = 0; i < count; ++i) {
        if (parts[i].type != api::pipeline_subobject_type::pixel_shader || parts[i].count != 1) continue;
        const auto &shader = *static_cast<const api::shader_desc *>(parts[i].data);
        ComPtr<ID3D11ShaderReflection> reflection;
        if (FAILED(D3DReflect(shader.code, shader.code_size, IID_PPV_ARGS(&reflection)))) return;
        D3D11_SHADER_DESC desc{};
        if (FAILED(reflection->GetDesc(&desc))) return;
        std::bitset<slots> used;
        for (unsigned j = 0; j < desc.BoundResources; ++j) {
            D3D11_SHADER_INPUT_BIND_DESC binding{};
            if (FAILED(reflection->GetResourceBindingDesc(j, &binding))) return;
            if (binding.Type != D3D_SIT_TEXTURE || binding.BindPoint >= slots) continue;
            const unsigned limit = std::min<unsigned>(binding.BindCount, unsigned(slots) - binding.BindPoint);
            for (unsigned k = 0; k < limit; ++k) used.set(binding.BindPoint + k);
        }
        std::lock_guard lock(gate);
        shader_slots[pipeline.handle] = used;
    }
}

void destroy_texture(api::device *, api::resource r) {
    if (!done.load() || rearm_sidecar.load()) {
        std::lock_guard lock(gate);
        textures.erase(r.handle);
        if (fan_enabled) fan_textures.erase(r.handle);
    }
}
void destroy_pipeline(api::device *device, api::pipeline p) {
    if (draw_packet_enabled || fan_packet_enabled) dp::forget(device, p);
    if (!done.load() || rearm_sidecar.load()) { std::lock_guard lock(gate); shader_slots.erase(p.handle); }
}
void destroy_device(api::device *device) {
    if (draw_packet_enabled || fan_packet_enabled) dp::forget(device);
}

D3D11_RENDER_TARGET_BLEND_DESC read_blend(ID3D11DeviceContext *context) {
    ComPtr<ID3D11BlendState> state;
    context->OMGetBlendState(&state, nullptr, nullptr);
    if (state) {
        D3D11_BLEND_DESC desc{};
        state->GetDesc(&desc);
        return desc.RenderTarget[0];
    }
    return {FALSE, D3D11_BLEND_ONE, D3D11_BLEND_ZERO, D3D11_BLEND_OP_ADD,
            D3D11_BLEND_ONE, D3D11_BLEND_ZERO, D3D11_BLEND_OP_ADD, D3D11_COLOR_WRITE_ENABLE_ALL};
}

bool rgb_blend_consistent(const D3D11_RENDER_TARGET_BLEND_DESC &blend, bool screen) {
    if (!blend.BlendEnable || blend.BlendOp != D3D11_BLEND_OP_ADD) return false;
    if (screen) {
        return (blend.SrcBlend == D3D11_BLEND_ONE && blend.DestBlend == D3D11_BLEND_INV_SRC_COLOR) ||
               (blend.SrcBlend == D3D11_BLEND_INV_DEST_COLOR && blend.DestBlend == D3D11_BLEND_ONE);
    }
    return blend.DestBlend == D3D11_BLEND_ONE &&
           (blend.SrcBlend == D3D11_BLEND_ONE || blend.SrcBlend == D3D11_BLEND_SRC_ALPHA);
}

struct capture_match { int index = -1; const char *rule = nullptr; };

unsigned texture_mask(const draw &row) {
    unsigned used = 0;
    for (const auto &match : row.matches) used |= 1u << match.texture;
    return used;
}

bool request_screen_packet() {
    return session_shot % 2 == 0;
}

const char *packet_layer() {
    return request_screen_packet() ? "definition_2_outer_screen" : "definition_0_indexed_alpha_add";
}

bool has_packet_textures(const draw &row) {
    return row.reflected && texture_mask(row) == (request_screen_packet() ? 0x007u : 0x0c0u);
}

bool matches_packet_layer(const draw &row) {
    if (!has_packet_textures(row) || (row.blend.RenderTargetWriteMask & rgb_write_mask) != rgb_write_mask) return false;
    if (request_screen_packet()) return rgb_blend_consistent(row.blend, true);
    return row.args.indexed && rgb_blend_consistent(row.blend, false) && row.blend.SrcBlend == D3D11_BLEND_SRC_ALPHA;
}

bool alpha_over(const D3D11_RENDER_TARGET_BLEND_DESC &blend) {
    return blend.BlendEnable && blend.BlendOp == D3D11_BLEND_OP_ADD &&
           (blend.SrcBlend == D3D11_BLEND_SRC_ALPHA || blend.SrcBlend == D3D11_BLEND_ONE) &&
           blend.DestBlend == D3D11_BLEND_INV_SRC_ALPHA;
}

capture_match capture_profile(const draw &row, unsigned texture_set) {
    if ((row.blend.RenderTargetWriteMask & rgb_write_mask) != rgb_write_mask) return {};
    const auto used = texture_set & ((1u << legacy_texture_count) - 1);
    const bool tower_body = used == 0x007;
    if (tower_body && rgb_blend_consistent(row.blend, true)) return {0, "authored_screen"};
    if (used == 0x00a && rgb_blend_consistent(row.blend, false)) return {1, "authored_add"};
    if (observed_ffxiv_capture && tower_body && row.matches.size() == 3 && row.args.indexed &&
        row.matches[0].slot == 0 && row.matches[0].texture == 0 && row.matches[1].slot == 1 &&
        row.matches[1].texture == 1 && row.matches[2].slot == 2 && row.matches[2].texture == 2 &&
        alpha_over(row.blend) && row.blend.SrcBlend == D3D11_BLEND_SRC_ALPHA)
        return {0, "observed_ffxiv_src_alpha_over"};
    return {};
}

bool exact_texture_set(unsigned used) {
    if ((used & (used - 1)) == 0) return false; // A single texture can be shared across definitions.
    return std::any_of(std::begin(tower_inventory::particles), std::end(tower_inventory::particles),
                       [&](const auto &particle) { return particle.texture_mask == used; });
}

capture_match first_open_capture(unsigned first, unsigned count, const char *rule, unsigned &dropped) {
    for (unsigned i = first; i < first + count; ++i) {
        if (!pixel_captures[i].sequence) return {static_cast<int>(i), rule};
    }
    ++dropped;
    return {};
}

capture_match select_capture(const draw &row, size_t sequence) {
    if (!pixel_capture_enabled) return {};
    const unsigned used = texture_mask(row);
    const auto core = capture_profile(row, used);
    if (core.index >= 0 && !pixel_captures[core.index].sequence) return core;
    if (!whole_tower || !(row.blend.RenderTargetWriteMask & rgb_write_mask) ||
        !row.blend.BlendEnable || row.blend.BlendOp != D3D11_BLEND_OP_ADD) return {};

    const auto last_anchor = std::max(pixel_captures[0].sequence, pixel_captures[1].sequence);
    if (used) {
        const auto *rule = exact_texture_set(used) ? "inventory_texture_set" : "inventory_partial_or_shared";
        if (!last_anchor) return first_open_capture(early_begin, early_slots, rule, early_budget_dropped);
        // After the first anchor, no future inventory draw can be classified as early.
        for (unsigned i = late_begin; i < neighbor_begin; ++i) {
            if (!pixel_captures[i].sequence) return {static_cast<int>(i), rule};
        }
        const auto spare = first_open_capture(early_begin, early_slots, rule, late_budget_dropped);
        if (spare.index >= 0) ++late_budget_borrowed;
        return spare;
    }
    if (pixel_captures[0].sequence && pixel_captures[1].sequence && sequence - last_anchor <= neighbor_distance &&
        row.reflected && row.bound_views && (rgb_blend_consistent(row.blend, false) ||
        rgb_blend_consistent(row.blend, true) || alpha_over(row.blend))) {
        return first_open_capture(neighbor_begin, neighbor_slots, "neighbor_after_anchors", neighbor_budget_dropped);
    }
    return {};
}

bool has_predication(ID3D11DeviceContext *context) {
    ComPtr<ID3D11Predicate> predicate;
    BOOL predicate_value = FALSE;
    context->GetPredication(&predicate, &predicate_value);
    return bool(predicate);
}

template<typename Interface, size_t Size>
std::bitset<Size> bound_slots(Interface *(&bindings)[Size]) {
    std::bitset<Size> active;
    for (size_t slot = 0; slot < Size; ++slot) {
        if (!bindings[slot]) continue;
        active.set(slot);
        bindings[slot]->Release();
    }
    return active;
}

bool stencil_ops_may_write(const D3D11_DEPTH_STENCILOP_DESC &ops) {
    return ops.StencilFailOp != D3D11_STENCIL_OP_KEEP ||
           ops.StencilDepthFailOp != D3D11_STENCIL_OP_KEEP ||
           ops.StencilPassOp != D3D11_STENCIL_OP_KEEP;
}

void inspect_depth_stencil(ID3D11DeviceContext *context, ID3D11DepthStencilView *view, draw_pipeline &pipeline) {
    ComPtr<ID3D11DepthStencilState> state;
    context->OMGetDepthStencilState(&state, nullptr);
    pipeline.default_depth_stencil_state = !state;
    D3D11_DEPTH_STENCIL_DESC desc = CD3D11_DEPTH_STENCIL_DESC(D3D11_DEFAULT);
    if (state) state->GetDesc(&desc);
    pipeline.depth_test = desc.DepthEnable != FALSE;
    pipeline.stencil_test = desc.StencilEnable != FALSE;
    pipeline.depth_stencil_bound = view != nullptr;
    if (!view) return;

    D3D11_DEPTH_STENCIL_VIEW_DESC view_desc{};
    view->GetDesc(&view_desc);
    pipeline.depth_stencil_format = view_desc.Format;
    pipeline.depth_stencil_view_flags = view_desc.Flags;
    pipeline.depth_may_write = !(view_desc.Flags & D3D11_DSV_READ_ONLY_DEPTH) &&
                               pipeline.depth_test && desc.DepthWriteMask == D3D11_DEPTH_WRITE_MASK_ALL;
    const bool has_stencil = view_desc.Format == DXGI_FORMAT_D24_UNORM_S8_UINT ||
                             view_desc.Format == DXGI_FORMAT_D32_FLOAT_S8X24_UINT;
    pipeline.stencil_may_write = has_stencil && !(view_desc.Flags & D3D11_DSV_READ_ONLY_STENCIL) &&
                                 pipeline.stencil_test && desc.StencilWriteMask != 0 &&
                                 (stencil_ops_may_write(desc.FrontFace) || stencil_ops_may_write(desc.BackFace));
}

draw_pipeline inspect_pipeline(ID3D11DeviceContext *context, bool predication) {
    draw_pipeline pipeline;
    pipeline.predication = predication;
    ID3D11RenderTargetView *render_targets[D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT]{};
    ComPtr<ID3D11DepthStencilView> depth_stencil;
    context->OMGetRenderTargets(static_cast<UINT>(std::size(render_targets)), render_targets, &depth_stencil);
    pipeline.render_targets = bound_slots(render_targets);
    inspect_depth_stencil(context, depth_stencil.Get(), pipeline);

    ComPtr<ID3D11Device> device;
    context->GetDevice(&device);
    const auto level = device->GetFeatureLevel();
    if (level >= D3D_FEATURE_LEVEL_11_0) {
        pipeline.inspected_uav_slots = level >= D3D_FEATURE_LEVEL_11_1 ? D3D11_1_UAV_SLOT_COUNT :
                                      D3D11_PS_CS_UAV_REGISTER_COUNT;
        ID3D11UnorderedAccessView *uavs[D3D11_1_UAV_SLOT_COUNT]{};
        context->OMGetRenderTargetsAndUnorderedAccessViews(0, nullptr, nullptr, 0, pipeline.inspected_uav_slots, uavs);
        pipeline.output_uavs = bound_slots(uavs);
    }
    ID3D11Buffer *stream_output[D3D11_SO_BUFFER_SLOT_COUNT]{};
    context->SOGetTargets(static_cast<UINT>(std::size(stream_output)), stream_output);
    pipeline.stream_output = bound_slots(stream_output);
    return pipeline;
}

void describe_surface(const D3D11_TEXTURE2D_DESC &desc, DXGI_FORMAT view_format, pixel_capture &capture) {
    capture.resource_format = desc.Format;
    capture.view_format = view_format;
    capture.samples = desc.SampleDesc.Count;
    capture.target_width = desc.Width;
    capture.target_height = desc.Height;
}

bool configure_surface_bytes(pixel_capture &capture) {
    DXGI_FORMAT typeless_format;
    unsigned bytes_per_pixel = 4;
    switch (capture.view_format) {
    case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:
    case DXGI_FORMAT_R8G8B8A8_UNORM:
        typeless_format = DXGI_FORMAT_R8G8B8A8_TYPELESS;
        capture.layout = "rgba8";
        break;
    case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
    case DXGI_FORMAT_B8G8R8A8_UNORM:
        typeless_format = DXGI_FORMAT_B8G8R8A8_TYPELESS;
        capture.layout = "bgra8";
        break;
    case DXGI_FORMAT_R16G16B16A16_FLOAT:
        typeless_format = DXGI_FORMAT_R16G16B16A16_TYPELESS;
        capture.layout = "rgba16f";
        bytes_per_pixel = 8;
        break;
    default: return false;
    }
    if (capture.resource_format != capture.view_format && capture.resource_format != typeless_format) return false;
    capture.srgb = capture.view_format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB ||
                   capture.view_format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
    capture.width = std::min(capture.target_width, pixel_edge_limit);
    capture.height = std::min(capture.target_height, pixel_edge_limit);
    capture.x = (capture.target_width - capture.width) / 2;
    capture.y = (capture.target_height - capture.height) / 2;
    capture.row_bytes = capture.width * bytes_per_pixel;
    return true;
}

bool create_staging(ID3D11DeviceContext *context, pixel_capture &capture) {
    D3D11_TEXTURE2D_DESC staging{};
    staging.Width = capture.width;
    staging.Height = capture.height;
    staging.MipLevels = staging.ArraySize = 1;
    // D3D11 copies compatible typed and typeless formats within one DXGI family.
    staging.Format = capture.view_format;
    staging.SampleDesc.Count = 1;
    staging.Usage = D3D11_USAGE_STAGING;
    staging.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Device> device;
    context->GetDevice(&device);
    if (FAILED(device->CreateTexture2D(&staging, nullptr, &capture.before)) ||
        (!capture.terminal && FAILED(device->CreateTexture2D(&staging, nullptr, &capture.after)))) {
        capture.before.Reset();
        capture.after.Reset();
        return false;
    }
    return true;
}

bool plain_surface(const D3D11_TEXTURE2D_DESC &desc) {
    return desc.ArraySize == 1 && desc.MipLevels == 1 && desc.MiscFlags == 0;
}

void copy_crop(ID3D11DeviceContext *context, ID3D11Texture2D *source, ID3D11Texture2D *destination,
               const pixel_capture &capture) {
    const D3D11_BOX box{capture.x, capture.y, 0, capture.x + capture.width, capture.y + capture.height, 1};
    context->CopySubresourceRegion(destination, 0, 0, 0, 0, source, 0, &box);
}

const char *stage_pixels(api::command_list *list, ID3D11DeviceContext *context, const draw_arguments &args,
                         const std::bitset<slots> &used_ps_slots, pixel_capture &capture,
                         ComPtr<ID3D11RenderTargetView> target = {}) {
    if (!args.count || !args.instances) return "unsupported_draw_arguments";
    if (context->GetType() != D3D11_DEVICE_CONTEXT_IMMEDIATE) return "unsupported_context";
    const bool fan = bool(target);
    if (capture.packet_selected && !capture.alpha_witness) {
        try {
            capture.packet_inputs = dp::input_inventory(context, args.indexed, used_ps_slots);
        } catch (const std::exception &) {}
    }
    // D3D11 also applies the predicate to copy commands.
    const bool predication = has_predication(context);
    if (whole_tower || fan) capture.pipeline = inspect_pipeline(context, predication);
    if (predication) return "unsupported_predication";

    if (!target) context->OMGetRenderTargets(1, &target, nullptr);
    if (!target) return "missing_rt0";
    D3D11_RENDER_TARGET_VIEW_DESC view{};
    target->GetDesc(&view);
    capture.view_format = view.Format;
    ComPtr<ID3D11Resource> resource;
    ComPtr<ID3D11Texture2D> texture;
    target->GetResource(&resource);
    if (FAILED(resource.As(&texture))) return "unsupported_rt0_resource";
    D3D11_TEXTURE2D_DESC desc{};
    texture->GetDesc(&desc);
    describe_surface(desc, view.Format, capture);
    if (desc.SampleDesc.Count != 1 || desc.SampleDesc.Quality != 0) return "unsupported_rt0_samples";
    if (view.ViewDimension != D3D11_RTV_DIMENSION_TEXTURE2D || view.Texture2D.MipSlice != 0 ||
        !plain_surface(desc)) return "unsupported_rt0_subresource";
    if (!configure_surface_bytes(capture)) return "unsupported_rt0_format";
    if (!create_staging(context, capture)) return "staging_create_failed";

    if (capture.packet_selected && capture.packet_status.empty()) {
        try {
            const auto &p = *capture.pipeline;
            dp::need(p.render_targets.to_ulong() == 1 && p.stream_output.none() && p.output_uavs.none() &&
                     (capture.alpha_witness || (!p.depth_may_write && !p.stencil_may_write)),
                     "unsupported_output_bindings_or_writes");
            dp::need(capture.view_format == DXGI_FORMAT_R16G16B16A16_FLOAT, "requires_linear_rgba16f");
            dp::header h;
            h.sequence = static_cast<UINT>(capture.sequence);
            h.width = desc.Width; h.height = desc.Height;
            h.x = capture.x; h.y = capture.y; h.w = capture.width; h.h = capture.height;
            h.indexed = args.indexed; h.count = args.count; h.instances = args.instances;
            h.first = args.first_element; h.base = args.base_vertex; h.firstInstance = args.first_instance;
            dp::packet_readbacks readbacks;
            dp::capture_device snapshot(context, capture.alpha_witness ? &readbacks : nullptr);
            snapshot.encode(h);
            capture.packet_bytes = std::move(snapshot.out.data);
            if (capture.alpha_witness) capture.packet_staging.emplace(std::move(readbacks));
            capture.packet_status = "staged";
        } catch (const std::exception &error) { capture.packet_status = error.what(); }
    }

    // ReShade 6.8's D3D11 command list calls the underlying context directly.
    // Returning true after this forward suppresses the wrapper's duplicate draw.
    copy_crop(context, texture.Get(), capture.before.Get(), capture);
    if (args.indexed) {
        list->draw_indexed(args.count, args.instances, args.first_element, args.base_vertex, args.first_instance);
    } else {
        list->draw(args.count, args.instances, args.first_element, args.first_instance);
    }
    capture.forwarded = true;
    copy_crop(context, texture.Get(), capture.after.Get(), capture);
    if (whole_tower || fan) capture.identity = {std::move(texture), std::move(target)};
    return "staged";
}

const char *stage_terminal(api::command_list *list, api::swapchain *swapchain, pixel_capture &capture) {
    if (!list) return "missing_present_context";
    auto *context = reinterpret_cast<ID3D11DeviceContext *>(list->get_native());
    if (!context) return "unsupported_present_context";
    // ReShade 6.8 protects this immediate context for the DXGI present event.
    // Confirm that protection is active before touching it from the Present thread.
    native_draw_scope native_scope(context, true);
    if (!native_scope.protected_context()) return "unsupported_present_context_threading";
    if (context->GetType() != D3D11_DEVICE_CONTEXT_IMMEDIATE) return "unsupported_present_context";
    if (has_predication(context)) return "unsupported_present_predication";
    capture.back_buffer_index = swapchain->get_current_back_buffer_index();
    const auto back_buffer = swapchain->get_back_buffer(capture.back_buffer_index);
    auto *resource = reinterpret_cast<ID3D11Resource *>(back_buffer.handle);
    if (!resource) return "missing_swapchain_buffer";
    ComPtr<ID3D11Texture2D> texture;
    if (FAILED(resource->QueryInterface(IID_PPV_ARGS(&texture)))) return "unsupported_swapchain_resource";
    D3D11_TEXTURE2D_DESC desc{};
    texture->GetDesc(&desc);
    describe_surface(desc, desc.Format, capture);
    if (desc.SampleDesc.Count != 1 || desc.SampleDesc.Quality != 0) return "unsupported_swapchain_samples";
    if (!plain_surface(desc)) return "unsupported_swapchain_subresource";
    if (!configure_surface_bytes(capture)) return "unsupported_swapchain_format";
    if (!create_staging(context, capture)) return "staging_create_failed";
    copy_crop(context, texture.Get(), capture.before.Get(), capture);
    capture.identity.texture = std::move(texture);
    return "staged";
}

bool supported_fan_rt0(DXGI_FORMAT format) {
    return format == DXGI_FORMAT_R16G16B16A16_FLOAT ||
           format == DXGI_FORMAT_R8G8B8A8_UNORM || format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB ||
           format == DXGI_FORMAT_B8G8R8A8_UNORM || format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
}

void observe_fan_draw(const draw &row, DXGI_FORMAT view_format) {
    ++fan_identified_draws;
    for (const auto &match : row.matches) {
        const auto found = std::find_if(fan_observations.begin(), fan_observations.end(), [&](const auto &entry) {
            const auto &a = entry.blend;
            const auto &b = row.blend;
            return entry.texture == match.texture && entry.slot == match.slot && entry.view_format == view_format &&
                   a.BlendEnable == b.BlendEnable && a.SrcBlend == b.SrcBlend && a.DestBlend == b.DestBlend &&
                   a.BlendOp == b.BlendOp && a.RenderTargetWriteMask == b.RenderTargetWriteMask;
        });
        if (found != fan_observations.end()) ++found->count;
        else if (fan_observations.size() < fan_observation_limit) {
            fan_observations.push_back({match.texture, match.slot, row.blend, view_format});
        } else ++fan_observations_dropped;
    }
}

bool record_fan(api::command_list *list, ID3D11DeviceContext *context, const native_draw_scope &native_scope,
                const draw_arguments &args, const std::bitset<slots> &used_slots, unsigned interval, size_t ordinal) {
    if (used_slots.none()) return false;

    std::array<ComPtr<ID3D11Resource>, slots> resources;
    unsigned bound_views = 0;
    for (unsigned first = 0; first < slots; ) {
        if (!used_slots.test(first)) { ++first; continue; }
        unsigned end = first + 1;
        while (end < slots && used_slots.test(end)) ++end;
        ID3D11ShaderResourceView *views[slots];
        context->PSGetShaderResources(first, end - first, views);
        for (unsigned i = first; i < end; ++i) {
            ComPtr<ID3D11ShaderResourceView> view;
            view.Attach(views[i - first]);
            if (!view) continue;
            ++bound_views;
            view->GetResource(&resources[i]);
        }
        first = end;
    }

    draw row{args, true, bound_views, GetCurrentThreadId(), {}, {}, ordinal};
    std::unique_lock lock(gate);
    if (!recording || immediate != list || presents != interval || !fan_active) return false;
    for (unsigned slot = 0; slot < slots; ++slot) {
        if (!resources[slot]) continue;
        const auto found = fan_textures.find(reinterpret_cast<uint64_t>(resources[slot].Get()));
        if (found != fan_textures.end()) row.matches.push_back({slot, found->second});
    }
    if (row.matches.empty()) return false;
    lock.unlock();

    row.blend = read_blend(context);
    ComPtr<ID3D11RenderTargetView> target;
    context->OMGetRenderTargets(1, &target, nullptr);
    D3D11_RENDER_TARGET_VIEW_DESC view{};
    if (target) target->GetDesc(&view);
    const auto used = texture_mask(row);
    const bool coverage = used & (1u << fan_coverage_texture);
    const bool lightning = used & (1u << fan_lightning_texture);
    const bool supplemental_add = used & (1u << fan_add_texture);
    const bool screen = rgb_blend_consistent(row.blend, true);
    const bool over = alpha_over(row.blend);
    const bool add = rgb_blend_consistent(row.blend, false);
    const bool full_rgb = (row.blend.RenderTargetWriteMask & rgb_write_mask) == rgb_write_mask;

    lock.lock();
    if (!recording || immediate != list || presents != interval || !fan_active) return false;
    observe_fan_draw(row, view.Format);
    if (!coverage && !lightning && !supplemental_add) return false;
    if (unsigned(coverage) + unsigned(lightning) + unsigned(supplemental_add) != 1 ||
        !native_scope.known_threading() || !(supplemental_add ? add : (screen || over || add)) ||
        !full_rgb || !supported_fan_rt0(view.Format)) {
        ++fan_rejected_draws;
        return false;
    }

    const unsigned first = coverage ? 0 : (lightning ? fan_coverage_slots : fan_add_begin);
    const unsigned count = coverage ? fan_coverage_slots : (lightning ? fan_lightning_slots : fan_add_slots);
    const auto *role = coverage ? "fan_coverage" : (lightning ? "fan_lightning" : "fan_decal_add");
    const auto match = first_open_capture(first, count, role, fan_budget_dropped);
    if (match.index < 0) return false;
    auto &entry = pixel_captures[match.index];
    entry.sequence = ordinal;
    pixel_capture capture;
    capture.sequence = ordinal;
    capture.rule = match.rule;
    capture.fan_expected_state = view.Format == DXGI_FORMAT_R16G16B16A16_FLOAT &&
                                 (coverage ? over : (lightning ? screen : add));
    if (sequence_witness_armed && coverage) {
        capture.packet_selected = capture.alpha_witness = true;
        if (!capture.fan_expected_state || row.blend.SrcBlend != D3D11_BLEND_SRC_ALPHA ||
            row.blend.SrcBlendAlpha != D3D11_BLEND_ZERO || row.blend.DestBlendAlpha != D3D11_BLEND_INV_SRC_ALPHA ||
            row.blend.BlendOpAlpha != D3D11_BLEND_OP_ADD || row.blend.RenderTargetWriteMask != D3D11_COLOR_WRITE_ENABLE_ALL)
            capture.packet_status = "unsupported_coverage_alpha_equation";
    }
    if (fan_packet_requested && coverage && capture.fan_expected_state && !fan_packet_attempt_sequence) {
        // Coverage may precede lightning. Spend the packet budget once and
        // retain the payload only through this draw's own present.
        fan_packet_attempt_present = interval + 1;
        fan_packet_attempt_sequence = ordinal;
        capture.packet_selected = true;
    }
    if (fan_light_packets_requested && (lightning || supplemental_add) && (screen || add) &&
        view.Format == DXGI_FORMAT_R16G16B16A16_FLOAT) {
        auto &sequence = fan_light_packet_sequences[screen ? 0 : 1];
        if (!sequence) {
            sequence = ordinal;
            entry.packet_selected = capture.packet_selected = true;
        }
    }
    capture.producer_cookie = thread_cookie();
    lock.unlock();
    capture.status = stage_pixels(list, context, args, used_slots, capture, std::move(target));
    const bool forwarded = capture.forwarded;
    lock.lock();
    if (recording && immediate == list && presents == interval && fan_active) {
        if (fan_packet_requested && capture.packet_selected && !capture.alpha_witness)
            fan_packet_attempt_detail = capture.packet_status.empty() ? capture.status : capture.packet_status;
        if (forwarded) {
            entry = std::move(capture);
            rows.push_back(std::move(row));
            ++fan_staged_draws[coverage ? 0 : (lightning ? 1 : 2)];
        } else {
            entry = {};
            if (!first_fan_stage_failure) first_fan_stage_failure = capture.status;
            ++fan_rejected_draws;
        }
    }
    lock.unlock();
    return forwarded;
}

// Detect a native draw callback that straddles an interval boundary. Its
// membership cannot be reconstructed from a later Present.
std::atomic<unsigned> active_fan_callbacks{0};
struct counted_fan_callback {
    bool active = false;
    ~counted_fan_callback() { if (active) --active_fan_callbacks; }
};

bool record(api::command_list *list, const draw_arguments &args) {
    counted_fan_callback callback;
    if (!recording.load()) {
        if (report_pending.load()) finish_report(list, false);
        return false;
    }
    unsigned interval;
    size_t ordinal;
    bool fan;
    {
        std::lock_guard lock(gate);
        if (!recording || !immediate || list->get_device() != immediate->get_device()) return false;
        if (list != immediate) { ++deferred; return false; }
        if (args.indexed) ++indexed_count; else ++draw_count;
        if (rows.size() >= max_rows) return false;
        fan = fan_active;
        if (fan && fan_textures.empty()) return false;
        if (fan) { ++active_fan_callbacks; callback.active = true; }
        ordinal = size_t(draw_count) + indexed_count;
        interval = presents;
    }
    auto *context = reinterpret_cast<ID3D11DeviceContext *>(list->get_native());
    native_draw_scope native_scope(context, pixel_capture_enabled);
    ComPtr<ID3D11PixelShader> shader;
    context->PSGetShader(&shader, nullptr, nullptr);
    std::bitset<slots> used_slots;
    bool reflected;
    {
        std::lock_guard lock(gate);
        const auto found = shader_slots.find(reinterpret_cast<uint64_t>(shader.Get()));
        reflected = found != shader_slots.end();
        if (reflected) used_slots = found->second;
        else if (fan && recording && presents == interval) ++fan_unreflected_draws;
    }
    if (fan) return record_fan(list, context, native_scope, args, used_slots, interval, ordinal);
    draw row{args, reflected, 0, GetCurrentThreadId(), read_blend(context), {}};
    std::array<ComPtr<ID3D11Resource>, slots> resources;
    ID3D11ShaderResourceView *views[slots]{};
    context->PSGetShaderResources(0, static_cast<UINT>(slots), views);
    for (unsigned slot = 0; slot < slots; ++slot) {
        ComPtr<ID3D11ShaderResourceView> view;
        view.Attach(views[slot]);
        if (!view) continue;
        ++row.bound_views;
        if (used_slots.test(slot)) view->GetResource(&resources[slot]);
    }
    std::unique_lock lock(gate);
    if (!recording || immediate != list || presents != interval || rows.size() >= max_rows) return false;
    for (unsigned slot = 0; slot < slots; ++slot) {
        if (!resources[slot]) continue;
        const auto found = textures.find(reinterpret_cast<uint64_t>(resources[slot].Get()));
        if (found != textures.end()) row.matches.push_back({slot, found->second});
    }
    const auto match = select_capture(row, rows.size() + 1);
    const bool packet_candidate = draw_packet_enabled && match.index >= 0 && matches_packet_layer(row) &&
        std::none_of(pixel_captures.begin(), pixel_captures.end(), [](const auto &p) { return p.packet_selected; });
    rows.push_back(std::move(row));
    if (match.index < 0 || pixel_captures[match.index].sequence != 0) return false;
    auto &entry = pixel_captures[match.index];
    pixel_capture capture;
    capture.sequence = rows.size();
    capture.rule = match.rule;
    entry.sequence = capture.sequence;
    entry.rule = capture.rule;
    entry.packet_selected = capture.packet_selected = packet_candidate;
    if (!native_scope.known_threading()) {
        entry.status = "unsupported_context_threading";
        return false;
    }
    capture.producer_cookie = thread_cookie();
    lock.unlock();
    capture.status = stage_pixels(list, context, args, used_slots, capture);
    const bool forwarded = capture.forwarded;
    lock.lock();
    if (recording && immediate == list && presents == interval) entry = std::move(capture);
    lock.unlock();
    return forwarded;
}
bool on_draw(api::command_list *c, uint32_t n, uint32_t instances, uint32_t first_vertex, uint32_t first_instance) {
    return record(c, {false, n, instances, first_vertex, 0, first_instance});
}
bool on_indexed(api::command_list *c, uint32_t n, uint32_t instances, uint32_t first_index, int32_t base_vertex,
                uint32_t first_instance) {
    return record(c, {true, n, instances, first_index, base_vertex, first_instance});
}

std::string host_name() {
    return module_path().filename().u8string();
}

// Publish only completed files. Keeping the temporary file on the same volume
// makes the rename atomic, and omitting REPLACE_EXISTING protects previous reports.
bool publish_new(const std::wstring &path, const std::string &data, HANDLE *retained_file = nullptr) {
    const auto tmp = path + L".part." + std::to_wstring(GetCurrentProcessId());
    const DWORD access = GENERIC_WRITE | (retained_file ? DELETE : 0);
    const DWORD share = retained_file ? FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE : 0;
    HANDLE file = CreateFileW(tmp.c_str(), access, share, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    DWORD written = 0;
    const bool wrote = WriteFile(file, data.data(), static_cast<DWORD>(data.size()), &written, nullptr) && written == data.size();
    if (!retained_file) CloseHandle(file);
    if (wrote && MoveFileExW(tmp.c_str(), path.c_str(), MOVEFILE_WRITE_THROUGH)) {
        if (retained_file) *retained_file = file;
        return true;
    }
    if (retained_file) CloseHandle(file);
    DeleteFileW(tmp.c_str());
    return false;
}

enum class staging_result { copied, busy, failed };

staging_result read_staging(ID3D11DeviceContext *context, ID3D11Texture2D *texture, unsigned row_bytes,
                            unsigned height, unsigned map_flags, std::string &data) {
    data.resize(size_t(row_bytes) * height);
    D3D11_MAPPED_SUBRESOURCE mapped{};
    const HRESULT mapped_result = context->Map(texture, 0, D3D11_MAP_READ, map_flags, &mapped);
    if (mapped_result == DXGI_ERROR_WAS_STILL_DRAWING) return staging_result::busy;
    if (FAILED(mapped_result)) return staging_result::failed;
    const bool valid = mapped.pData && mapped.RowPitch >= row_bytes;
    if (valid) {
        const auto *source = static_cast<const char *>(mapped.pData);
        for (unsigned y = 0; y < height; ++y)
            std::memcpy(data.data() + size_t(y) * row_bytes, source + size_t(y) * mapped.RowPitch, row_bytes);
    }
    context->Unmap(texture, 0);
    return valid ? staging_result::copied : staging_result::failed;
}

const char *publish_pixels(ID3D11DeviceContext *context, pixel_capture &capture, unsigned map_flags,
                           const std::wstring &output_path, bool count_fan_changes) {
    const std::wstring layout = std::filesystem::path(capture.layout).wstring();
    if (capture.terminal) {
        const auto path = output_path + L".swapchain." + layout;
        if (GetFileAttributesW(output_path.c_str()) != INVALID_FILE_ATTRIBUTES ||
            GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES) return "capture_files_exist";
        std::string data;
        const auto result = read_staging(context, capture.before.Get(), capture.row_bytes, capture.height, map_flags, data);
        if (result == staging_result::busy) return nullptr;
        if (result == staging_result::failed) return "staging_read_failed";
        if (!publish_new(path, data)) return "capture_publish_failed";
        capture.before_file = std::filesystem::path(path).filename().u8string();
        return "captured";
    }
    const auto base = output_path + L".draw-" + std::to_wstring(capture.sequence);
    const auto before_path = base + L".before." + layout;
    const auto after_path = base + L".after." + layout;
    if (GetFileAttributesW(output_path.c_str()) != INVALID_FILE_ATTRIBUTES ||
        GetFileAttributesW(before_path.c_str()) != INVALID_FILE_ATTRIBUTES ||
        GetFileAttributesW(after_path.c_str()) != INVALID_FILE_ATTRIBUTES) return "capture_files_exist";
    std::string before_data, after_data;
    const auto before = read_staging(context, capture.before.Get(), capture.row_bytes, capture.height, map_flags, before_data);
    if (before == staging_result::busy) return nullptr;
    if (before == staging_result::failed) return "staging_read_failed";
    const auto after = read_staging(context, capture.after.Get(), capture.row_bytes, capture.height, map_flags, after_data);
    if (after == staging_result::busy) return nullptr;
    if (after == staging_result::failed) return "staging_read_failed";
    if (count_fan_changes) {
        const size_t pixel_bytes = capture.row_bytes / capture.width;
        const size_t rgb_bytes = pixel_bytes * 3 / 4;
        unsigned changed = 0;
        for (size_t offset = 0; offset < before_data.size(); offset += pixel_bytes) {
            if (std::memcmp(before_data.data() + offset, after_data.data() + offset, rgb_bytes) != 0) ++changed;
        }
        capture.rgb_changed_pixels = changed;
    }
    HANDLE before_handle = INVALID_HANDLE_VALUE;
    if (!publish_new(before_path, before_data, &before_handle)) return "capture_publish_failed";
    if (!publish_new(after_path, after_data)) {
        // Delete only the file we created, even if its name has changed.
        FILE_DISPOSITION_INFO disposition{TRUE};
        const bool cleaned = SetFileInformationByHandle(before_handle, FileDispositionInfo, &disposition, sizeof(disposition));
        CloseHandle(before_handle);
        return cleaned ? "capture_publish_failed" : "capture_cleanup_failed";
    }
    CloseHandle(before_handle);
    if (capture.packet_selected && !capture.packet_bytes.empty()) {
        const auto packet_path = base + L".packet";
        if (publish_new(packet_path, capture.packet_bytes)) {
            capture.packet_file = std::filesystem::path(packet_path).filename().u8string();
            if (capture.alpha_witness) capture.witness_file_bytes = capture.packet_bytes.size();
            capture.packet_status = "captured";
        } else capture.packet_status = "packet_publish_failed";
        std::string{}.swap(capture.packet_bytes);
    }
    capture.before_file = std::filesystem::path(before_path).filename().u8string();
    capture.after_file = std::filesystem::path(after_path).filename().u8string();
    return "captured";
}

void write_surface_fields(std::ostringstream &out, const pixel_capture &capture) {
    out << "\"status\":" << std::quoted(capture.status)
        << ",\"resourceFormat\":" << capture.resource_format << ",\"viewFormat\":" << capture.view_format
        << ",\"sampleCount\":" << capture.samples << ",\"targetWidth\":" << capture.target_width
        << ",\"targetHeight\":" << capture.target_height << ",\"x\":" << capture.x << ",\"y\":" << capture.y
        << ",\"width\":" << capture.width << ",\"height\":" << capture.height << ",\"rowBytes\":" << capture.row_bytes;
    if (capture.layout) out << ",\"layout\":" << std::quoted(capture.layout) << ",\"srgb\":" << capture.srgb;
    if (capture.readback_thread) out << ",\"readbackThreadId\":" << capture.readback_thread;
    if (capture.resource_id) out << ",\"targetResourceId\":" << capture.resource_id << ",\"mipSlice\":0,\"arraySlice\":0";
    if (capture.view_id) out << ",\"targetViewId\":" << capture.view_id;
}

template<size_t Size>
void write_bound_slots(std::ostringstream &out, const std::bitset<Size> &slots) {
    out << '[';
    const char *separator = "";
    for (size_t i = 0; i < Size; ++i) {
        if (!slots[i]) continue;
        out << separator << i;
        separator = ",";
    }
    out << ']';
}

void write_draw_pipeline(std::ostringstream &out, const draw_pipeline &pipeline) {
    out << ",\"pipelineConstraints\":{\"context\":\"immediate\",\"predicationBound\":" << pipeline.predication
        << ",\"colorTargetCount\":" << pipeline.render_targets.count() << ",\"colorTargetSlots\":";
    write_bound_slots(out, pipeline.render_targets);
    out << ",\"inspectedOutputUavSlots\":" << pipeline.inspected_uav_slots << ",\"outputUavSlots\":";
    write_bound_slots(out, pipeline.output_uavs);
    out << ",\"streamOutputSlots\":";
    write_bound_slots(out, pipeline.stream_output);
    out << ",\"depthStencil\":{\"bound\":" << pipeline.depth_stencil_bound
        << ",\"viewFormat\":" << pipeline.depth_stencil_format << ",\"viewFlags\":" << pipeline.depth_stencil_view_flags
        << ",\"defaultState\":" << pipeline.default_depth_stencil_state
        << ",\"depthTestEnabled\":" << pipeline.depth_test << ",\"stencilTestEnabled\":" << pipeline.stencil_test
        << ",\"depthMayWrite\":" << pipeline.depth_may_write << ",\"stencilMayWrite\":" << pipeline.stencil_may_write
        << "},\"notAssessed\":[\"activeQueries\",\"shaderResourceDependencies\",\"gamePostProcessing\"]}";
}

void write_pixel_capture(std::ostringstream &out, const pixel_capture &capture) {
    out << ",\"pixelCapture\":{";
    write_surface_fields(out, capture);
    if (std::strcmp(capture.status, "capture_crossed_present") == 0) out << ",\"forwarded\":null";
    else out << ",\"forwarded\":" << capture.forwarded;
    out << ",\"rtIndex\":0";
    if (whole_tower || fan_active) out << ",\"stage\":\"draw_rt0_before_after\"";
    if (capture.rule) out << ",\"captureRule\":" << std::quoted(capture.rule);
    if (capture.rgb_changed_pixels) out << ",\"rgbByteChangedPixels\":" << *capture.rgb_changed_pixels;
    if (capture.pipeline) write_draw_pipeline(out, *capture.pipeline);
    if (capture.alpha_witness) {
        const bool captured = !capture.packet_file.empty();
        out << ",\"alphaWitness\":{\"method\":\"isolated_packet_alpha_one_v1\",\"status\":"
            << std::quoted(captured ? "captured" : "unavailable");
        if (captured) out << ",\"file\":" << std::quoted(capture.packet_file) << ",\"bytes\":" << capture.witness_file_bytes;
        else out << ",\"detail\":" << std::quoted(capture.packet_status.empty() ? capture.status : capture.packet_status);
        out << '}';
    } else if (capture.packet_selected) {
        out << ",\"drawPacket\":{\"status\":" << std::quoted(capture.packet_status.empty() || capture.packet_status == "staged" ? capture.status : capture.packet_status);
        if (!capture.packet_file.empty()) out << ",\"file\":" << std::quoted(capture.packet_file);
        if (!capture.packet_inputs.empty()) out << ",\"inputs\":" << capture.packet_inputs;
        out << '}';
    }
    if (!capture.before_file.empty()) out << ",\"beforeFile\":" << std::quoted(capture.before_file);
    if (!capture.after_file.empty()) out << ",\"afterFile\":" << std::quoted(capture.after_file);
    out << '}';
}

void write_terminal(std::ostringstream &out, const pixel_capture &capture) {
    out << ",\"terminalColor\":{";
    write_surface_fields(out, capture);
    out << ",\"stage\":\"swapchain_before_reshade_and_dxgi_present\",\"formatSource\":\"swapchain_resource_desc\""
        << ",\"backBufferIndex\":" << capture.back_buffer_index;
    if (!capture.before_file.empty()) out << ",\"file\":" << std::quoted(capture.before_file);
    out << '}';
}

bool announce_ready() {
    const auto volume = std::filesystem::path(report_path).root_path().wstring();
    if (GetDriveTypeW(volume.c_str()) != DRIVE_FIXED || GetFileAttributesW(report_path.c_str()) != INVALID_FILE_ATTRIBUTES)
        return false;
    UCHAR bytes[16]{};
    if (BCryptGenRandom(nullptr, bytes, sizeof(bytes), BCRYPT_USE_SYSTEM_PREFERRED_RNG) < 0) return false;
    std::ostringstream token;
    token << std::hex << std::setfill('0');
    for (auto b : bytes) token << std::setw(2) << unsigned(b);
    request_token = token.str();
    std::ostringstream notice;
    notice << "{\"hostExecutable\":" << std::quoted(host_name()) << ",\"processId\":" << GetCurrentProcessId()
           << ",\"configurationSource\":" << std::quoted(configuration_source);
    if (draw_packet_enabled) notice << ",\"requestedPacketLayer\":" << std::quoted(packet_layer());
    if (fan_enabled) notice << ",\"fanCastSupported\":true,\"fanCastTimeoutMs\":" << fan_timeout_ms
                            << ",\"fanSequenceSupported\":true,\"fanSequenceRequestTokenBound\":true,\"fanSequenceMaxFrames\":" << fan_sequence_max_frames
                            << ",\"fanSequenceAlphaWitnessSupported\":" << (fan_packet_enabled ? "true" : "false")
                            << ",\"fanLightPacketsSupported\":" << (fan_packet_enabled ? "true" : "false")
                            << ",\"fanCoveragePacketSupported\":" << (fan_packet_enabled ? "true" : "false");
    if (rearm_sidecar) notice << ",\"sessionShot\":" << session_shot;
    notice << ",\"token\":" << std::quoted(request_token) << "}\n";
    changes = FindFirstChangeNotificationW(std::filesystem::path(report_path).parent_path().c_str(), FALSE,
                                            FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_LAST_WRITE | FILE_NOTIFY_CHANGE_SIZE);
    return publish_new(report_path + L".ready.json", notice.str());
}

void stop_request_watch() {
    if (changes != INVALID_HANDLE_VALUE) {
        FindCloseChangeNotification(changes);
        changes = INVALID_HANDLE_VALUE;
    }
}

void advance_profile_shot() {
    stop_request_watch();
    ++session_shot;
    report_path = profile_report_base + L"-shot-" + std::to_wstring(session_shot) + L".json";
    request_path = report_path + L".arm";
    request_token.clear();
}

enum class arm_request { none, tower, fan, fan_coverage_packet, fan_light_packets, fan_sequence, cancel_fan };

arm_request read_request() {
    if (changes != INVALID_HANDLE_VALUE) {
        // Avoid per-frame file I/O while the user is still entering the studio.
        if (WaitForSingleObject(changes, 0) == WAIT_TIMEOUT) return arm_request::none;
        if (!FindNextChangeNotification(changes)) stop_request_watch();
    }
    HANDLE file = CreateFileW(request_path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                              nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return arm_request::none;
    char buffer[64]{};
    DWORD n = 0;
    bool read = ReadFile(file, buffer, sizeof(buffer), &n, nullptr);
    CloseHandle(file);
    if (!read || n == sizeof(buffer)) return arm_request::none;
    while (n && (buffer[n-1] == '\r' || buffer[n-1] == '\n')) --n;
    const std::string_view command(buffer, n);
    if (command.substr(0, request_token.size()) != request_token) return arm_request::none;
    const auto suffix = command.substr(request_token.size());
    if (suffix.empty()) return arm_request::tower;
    if (fan_enabled && suffix == ":fan") return arm_request::fan;
    if (fan_packet_enabled && suffix == ":fan-coverage-packet") return arm_request::fan_coverage_packet;
    if (fan_packet_enabled && suffix == ":fan-light-packets") return arm_request::fan_light_packets;
    constexpr std::string_view sequence_prefix = ":fan-sequence:";
    if (fan_enabled && suffix.substr(0, sequence_prefix.size()) == sequence_prefix) {
        const auto number = suffix.substr(sequence_prefix.size());
        unsigned frames = 0;
        const auto result = std::from_chars(number.data(), number.data() + number.size(), frames);
        if (result.ec == std::errc{} && result.ptr == number.data() + number.size() &&
            frames >= 2 && frames <= fan_sequence_max_frames) {
            requested_sequence_frames = frames;
            return arm_request::fan_sequence;
        }
    }
    if (fan_enabled && suffix == ":cancel") return arm_request::cancel_fan;
    return arm_request::none;
}

bool inventory_blend_consistent(const D3D11_RENDER_TARGET_BLEND_DESC &blend, tower_inventory::mode mode) {
    switch (mode) {
    case tower_inventory::mode::screen: return rgb_blend_consistent(blend, true);
    case tower_inventory::mode::add: return rgb_blend_consistent(blend, false);
    case tower_inventory::mode::blend: return alpha_over(blend);
    }
    return false;
}

void write_inventory(std::ostringstream &out, const draw &row, unsigned used) {
    const bool exact = exact_texture_set(used);
    out << ",\"towerInventory\":{\"classification\":\"" << (exact ? "exact_texture_set" : "partial_or_shared") << '"';
    const auto write_definitions = [&](bool matching_blend) {
        bool first = true;
        for (const auto &particle : tower_inventory::particles) {
            const bool matches = exact ? particle.texture_mask == used : (particle.texture_mask & used) != 0;
            if (!matches || (matching_blend && !inventory_blend_consistent(row.blend, particle.draw_mode))) continue;
            if (!first) out << ',';
            first = false;
            out << particle.definition;
        }
    };
    out << ",\"authoredDefinitions\":[";
    write_definitions(false);
    out << "],\"blendConsistentDefinitions\":[";
    write_definitions(true);
    out << "]}";
}

void write_packet_selection(std::ostringstream &out) {
    const auto sampled = std::find_if(pixel_captures.begin(), pixel_captures.end(),
                                      [](const auto &p) { return p.packet_selected; });
    const auto textures_matched = std::count_if(rows.begin(), rows.end(), has_packet_textures);
    const auto eligible = std::count_if(rows.begin(), rows.end(), matches_packet_layer);
    const char *status = "no_matching_draw_in_recorded_rows";
    if (sampled != pixel_captures.end()) {
        status = sampled->packet_file.empty() ? "selected_without_packet" : "captured";
    } else if (eligible) {
        status = "eligible_draw_outside_capture_budget";
    } else if (textures_matched) {
        status = "texture_match_without_required_draw_state";
    }
    out << ",\"drawPacketMode\":\"alternating_single_draw\",\"drawPacketSelection\":{\"requestedLayer\":"
        << std::quoted(packet_layer()) << ",\"status\":" << std::quoted(status)
        << ",\"matchedTextureDraws\":" << textures_matched << ",\"eligibleDraws\":" << eligible;
    if (sampled != pixel_captures.end()) out << ",\"sequence\":" << sampled->sequence;
    out << '}';
}

void write_packet_metadata(std::ostringstream &out) {
    const auto stats = dp::read_metadata_stats();
    constexpr std::array names{"inputLayout", "vertexShader", "pixelShader"};
    out << ",\"drawPacketMetadata\":{\"inputLayoutBudgetBytes\":" << dp::layout_budget
        << ",\"shaderBudgetBytes\":" << dp::shader_budget;
    if (dp::shader_cache_enabled) {
        out << ",\"shaderCache\":{\"fileOpened\":" << (dp::shader_cache_file.load() != INVALID_HANDLE_VALUE)
            << ",\"vertexBudgetBytes\":" << dp::shader_cache_budgets[0]
            << ",\"pixelBudgetBytes\":" << dp::shader_cache_budgets[1]
            << ",\"vertexEntryLimit\":" << dp::shader_cache_entry_limits[0]
            << ",\"pixelEntryLimit\":" << dp::shader_cache_entry_limits[1]
            << ",\"freeSpaceReserveBytes\":" << dp::shader_cache_free_reserve << '}';
    }
    for (size_t i = 0; i < stats.size(); ++i) {
        const auto &part = stats[i];
        out << ',' << std::quoted(names[i]) << ":{\"creationEvents\":" << part.creation_events
            << ",\"liveBytes\":" << part.live_bytes << ",\"budgetRejected\":" << part.budget_exceeded
            << ",\"unsupportedDescriptions\":" << part.unsupported_description
            << ",\"privateDataWriteFailures\":" << part.write_failed;
        if (dp::shader_cache_enabled && i != 0) {
            out << ",\"cacheBytes\":" << part.cache_bytes
                << ",\"cacheEntries\":" << part.cache_entries << ",\"cacheHits\":" << part.cache_hits
                << ",\"cacheUnavailable\":" << part.cache_unavailable << ",\"cacheLowSpaceRejected\":" << part.cache_low_space
                << ",\"cacheWriteFailures\":" << part.cache_write_failed << ",\"cacheHashFailures\":" << part.cache_hash_failed
                << ",\"cacheReadFailures\":" << part.cache_read_failed;
        }
        out << '}';
    }
    out << '}';
}

void write_fan_coverage_packet(std::ostringstream &out) {
    const char *status = "no_qualifying_coverage";
    if (fan_packet_attempt_sequence) {
        status = "unmatched_frame";
        if (sampled_present == fan_packet_attempt_present) {
            const auto candidate = std::find_if(pixel_captures.begin(), pixel_captures.end(), [](const auto &capture) {
                return capture.packet_selected && capture.sequence == fan_packet_attempt_sequence;
            });
            status = candidate != pixel_captures.end() && !candidate->packet_file.empty() ? "captured" : "unavailable";
        }
    }
    out << ",\"coveragePacket\":{\"status\":" << std::quoted(status);
    if (fan_packet_attempt_sequence) out << ",\"attemptedPresent\":" << fan_packet_attempt_present
                                         << ",\"attemptedSequence\":" << fan_packet_attempt_sequence;
    if (!fan_packet_attempt_detail.empty()) out << ",\"attemptStatus\":" << std::quoted(fan_packet_attempt_detail);
    out << '}';
}

void write_fan_light_packets(std::ostringstream &out) {
    out << ",\"lightPackets\":{\"maxPackets\":2";
    for (size_t i = 0; i < fan_light_packet_sequences.size(); ++i) {
        const auto sequence = fan_light_packet_sequences[i];
        const auto capture = std::find_if(pixel_captures.begin(), pixel_captures.end(), [&](const auto &p) {
            return p.packet_selected && p.sequence == sequence;
        });
        const bool saved = capture != pixel_captures.end() && !capture->packet_file.empty();
        out << (i == 0 ? ",\"screen\":{" : ",\"add\":{")
            << "\"status\":" << std::quoted(saved ? "captured" : sequence ? "unavailable" : "not_seen");
        if (sequence) out << ",\"sequence\":" << sequence;
        if (saved) out << ",\"file\":" << std::quoted(capture->packet_file);
        if (sequence && !saved) {
            const char *detail = "capture_not_retained";
            if (capture != pixel_captures.end())
                detail = capture->packet_status.empty() || capture->packet_status == "staged" ?
                         capture->status : capture->packet_status.c_str();
            out << ",\"detail\":" << std::quoted(detail);
        }
        out << '}';
    }
    out << '}';
}

void write_events(std::ostringstream &out, const std::vector<draw> &records,
                  const decltype(pixel_captures) &captures, bool fan_mode) {
    out << ",\"events\":[\n";
    for (size_t n = 0; n < records.size(); ++n) {
        const auto &r = records[n];
        const auto &args = r.args;
        const auto &b = r.blend;
        const auto sequence = fan_mode ? r.draw_ordinal : n + 1;
        std::bitset<std::size(fingerprints)> used;
        if (n) out << ",\n";
        out << "{\"sequence\":" << sequence << ",\"type\":\"" << (args.indexed ? "draw_indexed" : "draw")
            << "\",\"elementCount\":" << args.count << ",\"instances\":" << args.instances
            << ",\"firstElement\":" << args.first_element << ",\"baseVertex\":" << args.base_vertex
            << ",\"firstInstance\":" << args.first_instance
            << ",\"drawThreadId\":" << r.thread_id
            << ",\"blendEnabled\":" << bool(b.BlendEnable) << ",\"srcBlend\":" << b.SrcBlend
            << ",\"dstBlend\":" << b.DestBlend << ",\"blendOp\":" << b.BlendOp
            << ",\"srcAlphaBlend\":" << b.SrcBlendAlpha << ",\"dstAlphaBlend\":" << b.DestBlendAlpha
            << ",\"alphaBlendOp\":" << b.BlendOpAlpha << ",\"writeMask\":" << unsigned(b.RenderTargetWriteMask)
            << ",\"boundPsViews\":" << r.bound_views << ",\"psSlotsReflected\":" << r.reflected;
        if (fan_mode) {
            out << ",\"fanTextures\":[";
            for (size_t j = 0; j < r.matches.size(); ++j) {
                const auto &m = r.matches[j];
                if (j) out << ',';
                out << "{\"slot\":" << m.slot << ",\"name\":" << std::quoted(fan_inventory::fingerprints[m.texture].name) << '}';
            }
            out << ']';
            for (const auto &capture : captures) {
                if (capture.sequence != sequence || !capture.status) continue;
                const auto used_fan = texture_mask(r);
                const unsigned anchor = (used_fan & (1u << fan_coverage_texture)) ? fan_coverage_texture :
                    ((used_fan & (1u << fan_lightning_texture)) ? fan_lightning_texture : fan_add_texture);
                out << ",\"fanCandidate\":{\"role\":" << std::quoted(capture.rule)
                    << ",\"expectedBlendAndTarget\":" << capture.fan_expected_state << ",\"authoredParticleDefinitions\":[";
                bool comma = false;
                for (const auto &particle : fan_inventory::particles) {
                    if (particle.texture_mask != (1u << anchor)) continue;
                    if (comma) out << ',';
                    out << particle.definition;
                    comma = true;
                }
                out << "]}";
                write_pixel_capture(out, capture);
                break;
            }
            out << '}';
            continue;
        }
        out << ",\"towerTextures\":[";
        for (size_t j = 0; j < r.matches.size(); ++j) {
            const auto &m = r.matches[j];
            if (j) out << ',';
            out << "{\"slot\":" << m.slot << ",\"name\":\"" << fingerprints[m.texture].name << "\"}";
            used.set(m.texture);
        }
        out << "],\"towerCandidates\":[";
        const bool body = used.test(0) && used.test(1) && used.test(2);
        if (body) {
            out << "{\"authoredParticle\":2,\"expectedMode\":\"screen\",\"rgbBlendConsistent\":"
                << rgb_blend_consistent(b, true) << "}";
        }
        if (used.test(1) && used.test(3)) {
            if (body) out << ',';
            out << "{\"authoredParticle\":3,\"expectedMode\":\"add\",\"rgbBlendConsistent\":"
                << rgb_blend_consistent(b, false) << "}";
        }
        out << ']';
        if (whole_tower && used.any()) write_inventory(out, r, static_cast<unsigned>(used.to_ulong()));
        for (const auto &capture : captures) {
            if (capture.sequence == n + 1 && capture.status) write_pixel_capture(out, capture);
        }
        out << '}';
    }
    out << "\n]}\n";
}

void write_fan_observations(std::ostringstream &out) {
    out << ",\"observations\":[";
    for (size_t i = 0; i < fan_observations.size(); ++i) {
        const auto &entry = fan_observations[i];
        const auto &b = entry.blend;
        if (i) out << ',';
        out << "{\"texture\":" << std::quoted(fan_inventory::fingerprints[entry.texture].name)
            << ",\"slot\":" << entry.slot << ",\"count\":" << entry.count << ",\"viewFormat\":" << entry.view_format
            << ",\"blendEnabled\":" << bool(b.BlendEnable) << ",\"srcBlend\":" << b.SrcBlend << ",\"dstBlend\":" << b.DestBlend
            << ",\"blendOp\":" << b.BlendOp << ",\"writeMask\":" << unsigned(b.RenderTargetWriteMask) << '}';
    }
    out << ']';
}

void write_report(const api::resource_desc &back_buffer) {
    const auto &size = back_buffer.texture;
    std::ostringstream out;
    out << std::boolalpha << "{\n\"api\":\"D3D11\",\"sdkApi\":" << RESHADE_API_VERSION
        << ",\"hostExecutable\":" << std::quoted(host_name())
        << ",\"captureMode\":\"" << (manual ? "manual" : "present_number") << "\""
        << ",\"configurationSource\":" << std::quoted(configuration_source)
        << ",\"pixelCaptureMode\":" << std::quoted(pixel_capture_mode)
        << ",\"processId\":" << GetCurrentProcessId() << ",\"present\":" << sampled_present
        << ",\"armPresentThreadId\":" << arm_present_thread << ",\"sampledPresentThreadId\":" << sampled_present_thread
        << ",\"width\":" << size.width << ",\"height\":" << size.height
        << ",\"drawCount\":" << draw_count << ",\"drawIndexedCount\":" << indexed_count
        << ",\"deferredDrawsObserved\":" << deferred << ",\"detailRowsDropped\":" << (draw_count + indexed_count - rows.size())
        << ",\"recognizedLiveTextures\":" << textures.size();
    if (rearm_sidecar) out << ",\"sessionShot\":" << session_shot;
    if (draw_packet_enabled && !fan_active) {
        write_packet_selection(out);
        write_packet_metadata(out);
    }
    if (whole_tower && !fan_active) {
        out << ",\"wholeTowerMode\":true,\"wholeTowerBudget\":{\"maxDrawPairs\":" << terminal_index
            << ",\"earlyInventoryLimit\":" << early_slots << ",\"lateInventoryLimit\":" << late_slots
            << ",\"neighborLimit\":" << neighbor_slots << ",\"neighborDistance\":" << neighbor_distance
            << ",\"borrowedEarlySlots\":" << late_budget_borrowed
            << ",\"earlyInventoryDropped\":" << early_budget_dropped << ",\"lateInventoryDropped\":" << late_budget_dropped
            << ",\"neighborDropped\":" << neighbor_budget_dropped << '}';
        write_terminal(out, pixel_captures[terminal_index]);
    }
    if (fan_active) {
        const auto capture_failed = [](const pixel_capture &capture) {
            return (capture.sequence || capture.terminal) &&
                   (!capture.status || std::strcmp(capture.status, "captured") != 0);
        };
        const bool incomplete = sampled_present && std::any_of(pixel_captures.begin(), pixel_captures.end(), capture_failed);
        out << ",\"fanCast\":{\"status\":" << std::quoted(incomplete ? "readback_incomplete" : fan_status)
            << ",\"asset\":\"vfx/monster/gimmick3/eff/n4gb_b_g02c0a1.avfx\""
            << ",\"assetSha256\":" << std::quoted(fan_inventory::avfx_sha256)
            << ",\"timeoutMs\":" << fan_timeout_ms << ",\"elapsedMs\":" << fan_elapsed
            << ",\"waitedPresents\":" << fan_waited_presents << ",\"textureMatchedDraws\":" << fan_identified_draws
            << ",\"recognizedLiveTextures\":" << fan_textures.size()
            << ",\"rejectedAnchorDraws\":" << fan_rejected_draws << ",\"budgetDroppedDraws\":" << fan_budget_dropped
            << ",\"stagedCoverageDraws\":" << fan_staged_draws[0] << ",\"stagedLightningDraws\":" << fan_staged_draws[1]
            << ",\"stagedDecalAddDraws\":" << fan_staged_draws[2]
            << ",\"observationEntriesDropped\":" << fan_observations_dropped;
        if (sampled_present) {
            out << ",\"qualification\":" << std::quoted(fan_status)
                << ",\"anchors\":{\"coverageSequence\":" << fan_coverage_sequence
                << ",\"lightningSequence\":" << fan_lightning_sequence
                << ",\"shareTarget\":" << fan_anchors_share_target << '}';
        }
        write_fan_observations(out);
        if (fan_packet_requested) write_fan_coverage_packet(out);
        if (fan_light_packets_requested) write_fan_light_packets(out);
        out << '}';
        if (sampled_present) write_terminal(out, pixel_captures[terminal_index]);
        if (fan_packet_requested || fan_light_packets_requested) write_packet_metadata(out);
    }
    write_events(out, rows, pixel_captures, fan_active);
    if (!publish_new(report_path, out.str())) log::message(log::level::error, "Draw probe could not publish a new report.");
}

void finish_report(api::command_list *list, bool in_present_callback) {
    if (!report_pending.load()) return;
    std::array<ComPtr<ID3D11Texture2D>, pixel_captures.size() * 2> abandoned_copies;
    std::unique_lock lock(gate);
    if (!report_pending || readback_in_progress) return;

    const uint64_t now = GetTickCount64();
    const uint64_t cookie = list && list == immediate ? thread_cookie() : 0;
    std::array<bool, pixel_captures.size()> reads{};
    if (cookie && !readback_cancelled && now < readback_deadline) {
        for (size_t i = 0; i < pixel_captures.size(); ++i) {
            const auto &capture = pixel_captures[i];
            reads[i] = capture.before && capture.producer_cookie == cookie && now >= capture.next_poll;
        }
    }
    if (std::any_of(reads.begin(), reads.end(), [](bool read) { return read; })) {
        // Present may run on a different thread. Do the immediate-context maps
        // only on the thread which queued this pair on this same context.
        readback_in_progress = true;
        const bool protected_present_readback = in_present_callback && (whole_tower || fan_active);
        lock.unlock();
        auto *context = reinterpret_cast<ID3D11DeviceContext *>(list->get_native());
        const auto flags = in_present_callback && !protected_present_readback ? 0 : D3D11_MAP_FLAG_DO_NOT_WAIT;
        native_draw_scope native_scope(context, protected_present_readback);
        for (size_t i = 0; i < reads.size(); ++i) {
            if (!reads[i]) continue;
            auto &capture = pixel_captures[i];
            capture.readback_thread = GetCurrentThreadId();
            capture.next_poll = now + 8;
            const auto status = protected_present_readback && !native_scope.protected_context() ?
                "unsupported_present_context_threading" : publish_pixels(context, capture, flags, report_path, fan_active);
            if (!status) continue; // The GPU still owns a staging copy. Try on a later draw.
            capture.status = status;
            std::string{}.swap(capture.packet_bytes);
            capture.before.Reset();
            capture.after.Reset();
        }
        lock.lock();
        readback_in_progress = false;
    }

    if (readback_cancelled || GetTickCount64() >= readback_deadline) {
        for (size_t i = 0; i < pixel_captures.size(); ++i) {
            auto &capture = pixel_captures[i];
            if (!capture.before) continue;
            capture.status = readback_cancelled ? "readback_swapchain_destroyed" :
                (capture.readback_thread ? "staging_read_timed_out" : "readback_draw_thread_not_seen");
            std::string{}.swap(capture.packet_bytes);
            abandoned_copies[i * 2] = std::move(capture.before);
            abandoned_copies[i * 2 + 1] = std::move(capture.after);
        }
    }
    if (std::any_of(pixel_captures.begin(), pixel_captures.end(), [](const auto &capture) { return bool(capture.before); })) return;
    report_pending = false;
    write_report(sampled_back_buffer);
    rows.clear();
    fan_active = false;
    if (rearm_sidecar) {
        // The sampled interval is closed and all staging resources are detached.
        // Resource and shader identity tracking spans session shots.
        pixel_captures = {};
        draw_count = indexed_count = deferred = 0;
        early_budget_dropped = late_budget_dropped = neighbor_budget_dropped = late_budget_borrowed = 0;
        immediate = nullptr;
        readback_cancelled = false;
        advance_profile_shot();
        done = false;
    } else {
        textures.clear(); fan_textures.clear(); shader_slots.clear();
    }
}

void detach_sampled_identities(std::array<surface_identity, pixel_captures.size()> &identities,
                               decltype(pixel_captures) &captures) {
    // Hold strong references within this frame so that COM handle reuse cannot
    // merge unrelated resources or views. Release them before DXGI presents.
    for (size_t i = 0; i < captures.size(); ++i) {
        auto &capture = captures[i];
        if (!capture.identity.texture) continue;
        capture.resource_id = static_cast<unsigned>(i + 1);
        if (capture.identity.view) capture.view_id = static_cast<unsigned>(i + 1);
        for (size_t j = 0; j < i; ++j) {
            if (capture.identity.texture.Get() == identities[j].texture.Get()) capture.resource_id = captures[j].resource_id;
            if (capture.identity.view && capture.identity.view.Get() == identities[j].view.Get())
                capture.view_id = captures[j].view_id;
        }
        identities[i] = std::move(capture.identity);
    }
}

struct fan_anchors {
    const pixel_capture *coverage = nullptr, *lightning = nullptr;
};

fan_anchors select_fan_anchors() {
    fan_anchors best;
    int best_rank = -1;
    for (unsigned i = 0; i < fan_coverage_slots; ++i) {
        const auto &coverage = pixel_captures[i];
        if (!coverage.before) continue;
        for (unsigned j = fan_coverage_slots; j < fan_add_begin; ++j) {
            const auto &lightning = pixel_captures[j];
            if (!lightning.before) continue;
            const bool shared = coverage.identity.texture.Get() == lightning.identity.texture.Get();
            const int rank = 2 * (int(coverage.fan_expected_state) + int(lightning.fan_expected_state)) + int(shared);
            if (rank > best_rank) {
                best = {&coverage, &lightning};
                best_rank = rank;
            }
        }
    }
    return best;
}

void discard_fan_frame(decltype(pixel_captures) &discarded) {
    std::swap(pixel_captures, discarded);
    rows.clear();
    draw_count = indexed_count = deferred = 0;
}

void begin_fan_arm(arm_request request) {
    fan_active = true;
    fan_status = first_fan_stage_failure = nullptr;
    fan_unreflected_draws = 0;
    fan_packet_requested = request == arm_request::fan_coverage_packet;
    fan_light_packets_requested = request == arm_request::fan_light_packets;
    fan_light_packet_sequences = {};
    fan_packet_attempt_present = 0;
    fan_packet_attempt_sequence = 0;
    fan_packet_attempt_detail.clear();
    fan_started = GetTickCount64();
    fan_deadline = fan_started + fan_timeout_ms;
    fan_waited_presents = fan_rejected_draws = fan_budget_dropped = 0;
    fan_observations_dropped = fan_identified_draws = 0;
    fan_staged_draws = {};
    fan_coverage_sequence = fan_lightning_sequence = 0;
    fan_anchors_share_target = false;
    fan_observations.clear();
}

#include "fan_sequence_capture.hpp"

void on_present(api::command_queue *queue, api::swapchain *swapchain, const api::rect *,
                const api::rect *, uint32_t, const api::rect *) {
    if ((done.load() && !report_pending.load() && !rearm_sidecar.load()) ||
        queue->get_device()->get_api() != api::device_api::d3d11) return;
    LARGE_INTEGER present_time{};
    QueryPerformanceCounter(&present_time);
    std::unique_lock lock(gate);
    if (!chain) chain = swapchain;
    if (swapchain != chain) return;
    ++presents;
    if (sequence_capture.active) {
        sequence_present(lock, queue, swapchain, present_time.QuadPart);
        return;
    }
    if (done) {
        const bool read_on_present = whole_tower || fan_active;
        auto *list = read_on_present ? queue->get_immediate_command_list() : nullptr;
        lock.unlock();
        finish_report(list, read_on_present);
        return;
    }
    if (recording) {
        decltype(pixel_captures) discarded;
        bool terminal_needed = whole_tower;
        bool sampled = true;
        if (fan_active) {
            ++fan_waited_presents;
            const uint64_t now = GetTickCount64();
            const auto anchors = select_fan_anchors();
            const bool light_interval = fan_light_packet_sequences[0] || fan_light_packet_sequences[1];
            terminal_needed = false;
            if (read_request() == arm_request::cancel_fan) fan_status = "cancelled";
            else if (now >= fan_deadline) fan_status = "timed_out";
            else if (fan_light_packets_requested ? light_interval : anchors.coverage != nullptr) {
                if (fan_light_packets_requested) fan_status = "captured_light_interval";
                else fan_status = anchors.coverage->fan_expected_state && anchors.lightning->fan_expected_state ?
                                  "captured" : "observed_variant";
                if (anchors.coverage) {
                    fan_coverage_sequence = anchors.coverage->sequence;
                    fan_lightning_sequence = anchors.lightning->sequence;
                    fan_anchors_share_target = anchors.coverage->identity.texture.Get() == anchors.lightning->identity.texture.Get();
                }
                terminal_needed = true;
            }
            if (!terminal_needed) discard_fan_frame(discarded);
            if (!fan_status) {
                lock.unlock();
                return;
            }
            fan_elapsed = now - fan_started;
            sampled = terminal_needed;
            stop_request_watch();
            std::sort(rows.begin(), rows.end(), [](const auto &a, const auto &b) {
                return a.draw_ordinal < b.draw_ordinal;
            });
        }
        recording = false;
        done = true;
        sampled_present = sampled ? presents : 0;
        sampled_present_thread = sampled ? GetCurrentThreadId() : 0;
        auto *sampled_list = immediate;
        for (auto &capture : pixel_captures) {
            if (capture.sequence && !capture.status) capture.status = "capture_crossed_present";
        }
        lock.unlock();
        const auto back_buffer = swapchain->get_device()->get_resource_desc(swapchain->get_current_back_buffer());
        pixel_capture terminal;
        std::array<surface_identity, pixel_captures.size()> identities;
        if (terminal_needed) {
            terminal.terminal = true;
            terminal.producer_cookie = thread_cookie();
            terminal.status = stage_terminal(sampled_list == queue->get_immediate_command_list() ? sampled_list : nullptr,
                                             swapchain, terminal);
        }
        lock.lock();
        if (terminal_needed) {
            pixel_captures[terminal_index] = std::move(terminal);
            detach_sampled_identities(identities, pixel_captures);
        }
        sampled_back_buffer = back_buffer;
        readback_deadline = GetTickCount64() + 3000;
        report_pending = true;
        lock.unlock();
        identities = {};
        finish_report(sampled_list, true);
        return;
    }
    if (manual && request_token.empty()) {
        if (!announce_ready()) {
            done = true;
            rearm_sidecar = false;
            stop_request_watch();
            log::message(log::level::error, "Draw probe needs unused report and ready paths on a local drive.");
        }
        return;
    }
    const auto request = manual ? read_request() :
        (presents == target_present - 1 ? arm_request::tower : arm_request::none);
    if (request == arm_request::tower || request == arm_request::fan ||
        request == arm_request::fan_coverage_packet || request == arm_request::fan_light_packets ||
        request == arm_request::fan_sequence) {
        if (request != arm_request::tower) begin_fan_arm(request);
        else stop_request_watch();
        immediate = queue->get_immediate_command_list();
        arm_present_thread = GetCurrentThreadId();
        recording = true;
        if (request == arm_request::fan_sequence) begin_sequence(present_time.QuadPart);
    }
}
void destroy_swapchain(api::swapchain *swapchain, bool) {
    if (done.load() && !report_pending.load() && !rearm_sidecar.load()) return;
    decltype(pixel_captures) completed_captures;
    std::unique_lock lock(gate);
    if (chain != swapchain) return;
    if (sequence_capture.active) {
        sequence_capture.fault = "swapchain_destroyed";
        chain = nullptr;
        recording = false;
        if (!sequence_capture.busy) finish_sequence(lock);
        return;
    }
    if (done) {
        readback_cancelled = true;
        if (rearm_sidecar) { chain = nullptr; immediate = nullptr; }
        lock.unlock();
        finish_report(nullptr, false);
        return;
    }
    if (recording && fan_active) {
        recording = false;
        done = true;
        chain = nullptr;
        immediate = nullptr;
        fan_status = "swapchain_destroyed";
        fan_elapsed = GetTickCount64() - fan_started;
        sampled_present = sampled_present_thread = 0;
        sampled_back_buffer = {};
        readback_cancelled = true;
        stop_request_watch();
        discard_fan_frame(completed_captures);
        report_pending = true;
        lock.unlock();
        finish_report(nullptr, false);
        return;
    }
    if (manual && recording) {
        if (!rearm_sidecar) done = true;
        log::message(log::level::error, "The sampled swapchain changed during this frame.");
    }
    recording = false; chain = nullptr; immediate = nullptr;
    if (rearm_sidecar && !request_token.empty()) advance_profile_shot();
    if (!rearm_sidecar) presents = 0;
    draw_count = indexed_count = deferred = 0;
    early_budget_dropped = late_budget_dropped = neighbor_budget_dropped = late_budget_borrowed = 0;
    rows.clear();
    std::swap(pixel_captures, completed_captures);
}
} // namespace

extern "C" __declspec(dllexport) const char *NAME = "Green Screen draw probe";
extern "C" __declspec(dllexport) const char *DESCRIPTION = "Records one D3D11 present interval in an isolated test host.";
BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_ATTACH) {
        if (!register_addon(module)) return FALSE;
        bool allow_profile = false;
        const bool configured = configure_environment(allow_profile);
        if (allow_profile) {
            const auto host = module_path().filename().wstring();
            if (fixture_host(host) || test_client_host(host)) {
                profile_module = module;
                done = true;
                register_event<addon_event::init_device>(init_profile);
            }
        }
        if (configured || profile_module) {
            register_event<addon_event::init_resource>(init_texture);
            register_event<addon_event::destroy_resource>(destroy_texture);
            register_event<addon_event::init_pipeline>(init_pipeline);
            register_event<addon_event::destroy_pipeline>(destroy_pipeline);
            register_event<addon_event::destroy_device>(destroy_device);
            register_event<addon_event::draw>(on_draw);
            register_event<addon_event::draw_indexed>(on_indexed);
            register_event<addon_event::present>(on_present);
            register_event<addon_event::destroy_swapchain>(destroy_swapchain);
        }
    } else if (reason == DLL_PROCESS_DETACH) {
        stop_request_watch();
        unregister_addon(module);
        // At process exit Windows closes the last handle and deletes the file.
        if (!reserved) dp::close_shader_cache();
    }
    return TRUE;
}
