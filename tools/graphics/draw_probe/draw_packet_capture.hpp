#pragma once
#include "draw_packet_format.hpp"
#include <bcrypt.h>
#include <d3dcompiler.h>
#include <reshade.hpp>
#include <algorithm>
#include <array>
#include <atomic>
#include <bitset>
#include <map>
#include <mutex>
#include <cwchar>
#include <unordered_map>

namespace dp {
inline constexpr GUID metadata_key{0xfc4abbbf,0x418b,0x4c18,{0x9e,0xdc,0x82,0xb7,0x7d,0x68,0xa9,0x04}};
inline constexpr GUID shader_cache_key{0x298e2a19,0x5f04,0x45af,{0xbc,0xce,0xf2,0x01,0x90,0x2e,0x7d,0xc4}};
enum class metadata_kind { layout, vs, ps };
enum class metadata_failure : uint32_t {
    unsupported_description = 1, budget_exceeded, write_failed,
    cache_unavailable, cache_low_space, cache_write_failed, cache_hash_failed
};
struct metadata_stats {
    size_t live_bytes = 0;
    uint64_t creation_events = 0, budget_exceeded = 0, unsupported_description = 0, write_failed = 0;
    uint64_t cache_bytes = 0, cache_entries = 0, cache_hits = 0, cache_unavailable = 0;
    uint64_t cache_low_space = 0, cache_write_failed = 0, cache_hash_failed = 0, cache_read_failed = 0;
};
struct metadata_reservation {
    reshade::api::device *device;
    metadata_kind kind;
    UINT size;
};
inline std::mutex metadata_gate;
inline std::array<metadata_stats, 3> metadata_totals;
inline std::unordered_map<uint64_t, metadata_reservation> metadata_reservations;
inline constexpr size_t metadata_mib = 1024u * 1024;
inline constexpr size_t layout_budget = 8 * metadata_mib, shader_budget = 88 * metadata_mib;
inline constexpr std::array<size_t, 2> shader_cache_budgets{256 * metadata_mib, 1792 * metadata_mib};
inline constexpr std::array<size_t, 2> shader_cache_entry_limits{32768, 131072};
inline constexpr size_t shader_cache_free_reserve = 512 * metadata_mib;
inline constexpr size_t shader_cache_space_check_interval = 16 * metadata_mib;

using shader_digest = std::array<UCHAR, 32>;
struct shader_location { uint64_t offset; UINT size; };
struct shader_reference {
    uint64_t offset;
    UINT size;
    metadata_kind kind;
    shader_digest digest;
};
inline std::array<std::map<shader_digest, shader_location>, 2> shader_cache_index;
inline bool shader_cache_enabled = false;
inline std::atomic<HANDLE> shader_cache_file{INVALID_HANDLE_VALUE};
inline std::wstring shader_cache_volume;
inline uint64_t shader_cache_size = 0, shader_cache_space_before_check = 0;
inline bool shader_cache_append_failed = false;

inline metadata_stats &totals(metadata_kind kind) { return metadata_totals[static_cast<size_t>(kind)]; }

inline std::array<metadata_stats, 3> read_metadata_stats() {
    std::lock_guard lock(metadata_gate);
    return metadata_totals;
}

// Called once after resolving the sidecar report directory, before pipeline events.
inline void open_shader_cache(const std::wstring &path) {
    shader_cache_enabled = true;
    // Keep an exclusive, non-inherited handle and never reopen a cache by name.
    const HANDLE file = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE | DELETE, 0, nullptr,
                                    CREATE_NEW, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT |
                                    FILE_FLAG_DELETE_ON_CLOSE, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;
    wchar_t resolved[32768]{}, volume[32768]{};
    const DWORD length = GetFinalPathNameByHandleW(file, resolved, DWORD(std::size(resolved)),
                                                   FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    // Verify the opened file resides on a fixed local volume before writing any bytecode.
    if (GetFileType(file) != FILE_TYPE_DISK || length < 7 || length >= std::size(resolved) ||
        std::wcsncmp(resolved, L"\\\\?\\", 4) != 0 || resolved[5] != L':' || resolved[6] != L'\\' ||
        !GetVolumePathNameW(resolved, volume, DWORD(std::size(volume))) || GetDriveTypeW(volume) != DRIVE_FIXED) {
        CloseHandle(file);
        return;
    }
    try {
        shader_cache_volume = volume;
    } catch (const std::bad_alloc &) {
        CloseHandle(file);
        return;
    }
    shader_cache_file.store(file);
}

inline void close_shader_cache() {
    const HANDLE file = shader_cache_file.exchange(INVALID_HANDLE_VALUE);
    if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
}

inline bool hash_shader(const void *data, UINT size, shader_digest &digest) {
    return BCryptHash(BCRYPT_SHA256_ALG_HANDLE, nullptr, 0,
                      const_cast<PUCHAR>(static_cast<const UCHAR *>(data)), size,
                      digest.data(), ULONG(digest.size())) >= 0;
}

inline bool seek_shader_cache(HANDLE file, uint64_t offset) {
    LARGE_INTEGER position{};
    position.QuadPart = offset;
    return SetFilePointerEx(file, position, nullptr, FILE_BEGIN) != FALSE;
}

inline metadata_failure cache_shader(metadata_kind kind, const void *data, UINT size, shader_reference &ref) {
    shader_digest digest{};
    const bool hashed = size && hash_shader(data, size, digest);
    std::lock_guard lock(metadata_gate);
    auto &stats = totals(kind);
    ++stats.creation_events;
    if (!size) {
        ++stats.unsupported_description;
        return metadata_failure::unsupported_description;
    }
    const HANDLE file = shader_cache_file.load();
    if (file == INVALID_HANDLE_VALUE) {
        ++stats.cache_unavailable;
        return metadata_failure::cache_unavailable;
    }
    if (!hashed) {
        ++stats.cache_hash_failed;
        return metadata_failure::cache_hash_failed;
    }
    const size_t stage = kind == metadata_kind::vs ? 0 : 1;
    auto &index = shader_cache_index[stage];
    const auto existing = index.find(digest);
    if (existing != index.end()) {
        if (existing->second.size != size) {
            ++stats.cache_hash_failed;
            return metadata_failure::cache_hash_failed;
        }
        ref = {existing->second.offset, size, kind, digest};
        ++stats.cache_hits;
        return {};
    }
    if (size > shader_cache_budgets[stage] - stats.cache_bytes || index.size() >= shader_cache_entry_limits[stage]) {
        ++stats.budget_exceeded;
        return metadata_failure::budget_exceeded;
    }
    if (shader_cache_append_failed) {
        ++stats.cache_write_failed;
        return metadata_failure::cache_write_failed;
    }
    if (size > shader_cache_space_before_check) {
        ULARGE_INTEGER available{};
        if (!GetDiskFreeSpaceExW(shader_cache_volume.c_str(), &available, nullptr, nullptr) ||
            available.QuadPart < shader_cache_free_reserve + size) {
            ++stats.cache_low_space;
            return metadata_failure::cache_low_space;
        }
        shader_cache_space_before_check = std::min<uint64_t>(shader_cache_space_check_interval,
                                                            available.QuadPart - shader_cache_free_reserve);
    }
    try {
        index.emplace(digest, shader_location{shader_cache_size, size});
    } catch (const std::bad_alloc &) {
        ++stats.cache_write_failed;
        return metadata_failure::cache_write_failed;
    }
    DWORD written = 0;
    if (!seek_shader_cache(file, shader_cache_size) || !WriteFile(file, data, size, &written, nullptr) || written != size) {
        index.erase(digest);
        shader_cache_append_failed = true;
        ++stats.cache_write_failed;
        return metadata_failure::cache_write_failed;
    }
    ref = {shader_cache_size, size, kind, digest};
    shader_cache_size += size;
    shader_cache_space_before_check -= size;
    stats.cache_bytes += size;
    ++stats.cache_entries;
    return {};
}

inline std::string cached_shader(metadata_kind kind, const shader_reference &ref, const char *prefix) {
    std::lock_guard lock(metadata_gate);
    const HANDLE file = shader_cache_file.load();
    std::string data;
    if (file != INVALID_HANDLE_VALUE && ref.kind == kind && ref.size > sizeof(metadata_failure) &&
        ref.size <= metadata_mib && ref.offset <= shader_cache_size && ref.size <= shader_cache_size - ref.offset) {
        DWORD read = 0;
        data.resize(ref.size);
        shader_digest digest{};
        if (seek_shader_cache(file, ref.offset) && ReadFile(file, data.data(), ref.size, &read, nullptr) &&
            read == ref.size && hash_shader(data.data(), ref.size, digest) && digest == ref.digest) return data;
    }
    ++totals(kind).cache_read_failed;
    throw std::runtime_error(std::string(prefix) + "_cache_read_failed");
}

inline void forget(reshade::api::device *device, reshade::api::pipeline pipeline) {
    std::lock_guard lock(metadata_gate);
    const auto record = metadata_reservations.find(pipeline.handle);
    if (record == metadata_reservations.end() || record->second.device != device) return;
    totals(record->second.kind).live_bytes -= record->second.size;
    metadata_reservations.erase(record);
}

inline void forget(reshade::api::device *device) {
    std::lock_guard lock(metadata_gate);
    for (auto record = metadata_reservations.begin(); record != metadata_reservations.end();) {
        if (record->second.device != device) { ++record; continue; }
        totals(record->second.kind).live_bytes -= record->second.size;
        record = metadata_reservations.erase(record);
    }
}

// The ledger only tracks the budget. The captured native object owns its metadata.
// The layout reserve is independent of shader churn. A four-byte value records a failed capture.
inline void store_metadata(reshade::api::device *device, reshade::api::pipeline pipeline, metadata_kind kind,
                           const void *data, UINT size) {
    auto *child = reinterpret_cast<ID3D11DeviceChild *>(pipeline.handle);
    if (shader_cache_enabled && kind != metadata_kind::layout) {
        shader_reference ref{};
        auto failure = cache_shader(kind, data, size, ref);
        if (failure == metadata_failure{} && SUCCEEDED(child->SetPrivateData(shader_cache_key, sizeof(ref), &ref))) return;
        if (failure == metadata_failure{}) {
            std::lock_guard lock(metadata_gate);
            ++totals(kind).write_failed;
            failure = metadata_failure::write_failed;
        }
        // Keep the complete reference or a failure on the actual native shader object.
        child->SetPrivateData(shader_cache_key, sizeof(failure), &failure);
        return;
    }
    metadata_failure failure{};
    {
        std::lock_guard lock(metadata_gate);
        auto &stats = totals(kind);
        ++stats.creation_events;
        if (metadata_reservations.find(pipeline.handle) != metadata_reservations.end()) return;
        const bool layout = kind == metadata_kind::layout;
        const size_t used = layout ? stats.live_bytes :
            totals(metadata_kind::vs).live_bytes + totals(metadata_kind::ps).live_bytes;
        const size_t budget = layout ? layout_budget : shader_budget;
        if (!size) {
            ++stats.unsupported_description;
            failure = metadata_failure::unsupported_description;
        } else if (size > budget - used) {
            ++stats.budget_exceeded;
            failure = metadata_failure::budget_exceeded;
        }
        const UINT charge = failure == metadata_failure{} ? size : 0;
        metadata_reservations.emplace(pipeline.handle, metadata_reservation{device, kind, charge});
        stats.live_bytes += charge;
    }

    // Never hold the ledger lock across a D3D11 call or from a destruction callback.
    if (failure != metadata_failure{}) {
        child->SetPrivateData(metadata_key, sizeof(failure), &failure);
        forget(device, pipeline);
    } else if (FAILED(child->SetPrivateData(metadata_key, size, data))) {
        failure = metadata_failure::write_failed;
        child->SetPrivateData(metadata_key, sizeof(failure), &failure);
        forget(device, pipeline);
        std::lock_guard lock(metadata_gate);
        ++totals(kind).write_failed;
    }
}

inline void remember(reshade::api::device *device, const reshade::api::pipeline_subobject *parts,
                     UINT count, reshade::api::pipeline pipeline) {
    using namespace reshade::api;
    if (!pipeline.handle || !parts || !count) return;
    for (UINT i = 0; i < count; ++i) {
        if (parts[i].type == pipeline_subobject_type::input_layout) {
            // ReShade reports the layout alongside the vertex signature it was created with.
            // The handle belongs to the layout in this event, even when a shader descriptor is present.
            const auto &part = parts[i];
            std::array<element, D3D11_IA_VERTEX_INPUT_STRUCTURE_ELEMENT_COUNT> encoded{};
            UINT size = 0;
            if (part.data && part.count && part.count <= encoded.size()) {
                const auto *inputs = static_cast<const input_element *>(part.data);
                size = part.count * sizeof(element);
                for (UINT j = 0; j < part.count; ++j) {
                    const auto &input = inputs[j];
                    auto &output = encoded[j];
                    const size_t length = strnlen_s(input.semantic, sizeof(output.name));
                    if (!length || length == sizeof(output.name)) { size = 0; break; }
                    std::memcpy(output.name, input.semantic, length);
                    output.index = input.semantic_index;
                    output.slot = input.buffer_binding;
                    output.offset = input.offset;
                    output.fmt = static_cast<UINT>(input.format);
                    output.step = input.instance_step_rate;
                }
            }
            store_metadata(device, pipeline, metadata_kind::layout, encoded.data(), size);
            return;
        }
    }
    if (count != 1) return;
    const bool vs = parts->type == pipeline_subobject_type::vertex_shader;
    if (!vs && parts->type != pipeline_subobject_type::pixel_shader) return;
    if (parts->count == 1 && parts->data) {
        const auto &shader = *static_cast<const shader_desc *>(parts->data);
        if (shader.code && shader.code_size > sizeof(metadata_failure) && shader.code_size <= metadata_mib) {
            store_metadata(device, pipeline, vs ? metadata_kind::vs : metadata_kind::ps,
                           shader.code, static_cast<UINT>(shader.code_size));
            return;
        }
    }
    store_metadata(device, pipeline, vs ? metadata_kind::vs : metadata_kind::ps, nullptr, 0);
}

inline std::string metadata(ID3D11DeviceChild *object, const char *prefix, metadata_kind kind = metadata_kind::layout) {
    const bool cached = shader_cache_enabled && kind != metadata_kind::layout;
    const GUID &key = cached ? shader_cache_key : metadata_key;
    UINT size = 0;
    const auto result = object ? object->GetPrivateData(key, &size, nullptr) : DXGI_ERROR_NOT_FOUND;
    if (result == DXGI_ERROR_NOT_FOUND || (SUCCEEDED(result) && !size))
        throw std::runtime_error(std::string(prefix) + "_missing");
    if ((FAILED(result) && result != DXGI_ERROR_MORE_DATA) || !size || size > metadata_mib ||
        (cached && size != sizeof(shader_reference) && size != sizeof(metadata_failure)))
        throw std::runtime_error(std::string(prefix) + "_read_failed");
    std::string data(size, '\0');
    if (FAILED(object->GetPrivateData(key, &size, data.data())) || size != data.size())
        throw std::runtime_error(std::string(prefix) + "_read_failed");
    if (size == sizeof(metadata_failure)) {
        metadata_failure failure;
        std::memcpy(&failure, data.data(), sizeof(failure));
        switch (failure) {
        case metadata_failure::unsupported_description:
            throw std::runtime_error(std::string(prefix) + "_unsupported_description");
        case metadata_failure::budget_exceeded:
            throw std::runtime_error(std::string(prefix) + "_budget_exceeded");
        case metadata_failure::write_failed:
            throw std::runtime_error(std::string(prefix) + "_write_failed");
        case metadata_failure::cache_unavailable:
            throw std::runtime_error(std::string(prefix) + "_cache_unavailable");
        case metadata_failure::cache_low_space:
            throw std::runtime_error(std::string(prefix) + "_cache_low_space");
        case metadata_failure::cache_write_failed:
            throw std::runtime_error(std::string(prefix) + "_cache_write_failed");
        case metadata_failure::cache_hash_failed:
            throw std::runtime_error(std::string(prefix) + "_cache_hash_failed");
        default:
            throw std::runtime_error(std::string(prefix) + "_read_failed");
        }
    }
    if (cached) {
        shader_reference ref{};
        std::memcpy(&ref, data.data(), sizeof(ref));
        return cached_shader(kind, ref, prefix);
    }
    return data;
}
using vertex_slots = std::bitset<D3D11_IA_VERTEX_INPUT_RESOURCE_SLOT_COUNT>;

inline vertex_slots layout_vertex_slots(const std::string &elements) {
    need(elements.size() % sizeof(element) == 0 &&
         elements.size() / sizeof(element) <= D3D11_IA_VERTEX_INPUT_STRUCTURE_ELEMENT_COUNT,
         "invalid_input_layout_metadata");
    vertex_slots used;
    for (size_t offset = 0; offset < elements.size(); offset += sizeof(element)) {
        element input;
        std::memcpy(&input, elements.data() + offset, sizeof(input));
        need(input.slot < used.size(), "invalid_input_layout_slot");
        used.set(input.slot);
    }
    return used;
}

struct mapped_read {
    ID3D11DeviceContext *context;
    ID3D11Resource *resource;
    UINT mip;
    D3D11_MAPPED_SUBRESOURCE map{};
    mapped_read(ID3D11DeviceContext *c,ID3D11Resource *r,UINT m) : context(c),resource(r),mip(m) {
        hr(c->Map(r,m,D3D11_MAP_READ,0,&map),"readback_map_failed");
    }
    ~mapped_read() { context->Unmap(resource,mip); }
};

// Copy mutable inputs at their original draw. Read only the private staging
// resources a few presents later, after the GPU has had time to finish them.
struct packet_readbacks {
    struct plane {
        ComPtr<ID3D11Resource> staging;
        UINT mip, first, pitch, rows;
        size_t offset;
    };
    std::vector<plane> planes;
    size_t staging_bytes = 0, next = 0;

    void reserve_staging(size_t bytes) {
        need(bytes <= limit - staging_bytes, "packet_staging_budget");
        staging_bytes += bytes;
    }

    void add(writer &out, ID3D11Resource *resource, UINT mip, UINT first, UINT pitch, UINT rows) {
        const size_t size = size_t(pitch) * rows;
        const size_t offset = out.data.size();
        need(size <= limit - offset, "packet_byte_budget");
        out.data.resize(offset + size);
        planes.push_back({resource, mip, first, pitch, rows, offset});
    }

    bool collect(ID3D11DeviceContext *context, std::string &data) {
        for (; next < planes.size(); ++next) {
            auto &p = planes[next];
            D3D11_MAPPED_SUBRESOURCE mapped{};
            const HRESULT result = context->Map(p.staging.Get(), p.mip, D3D11_MAP_READ,
                                                D3D11_MAP_FLAG_DO_NOT_WAIT, &mapped);
            if (result == DXGI_ERROR_WAS_STILL_DRAWING) return false;
            hr(result, "readback_map_failed");
            const bool valid = mapped.pData && (p.rows == 1 || mapped.RowPitch >= p.pitch);
            if (valid) {
                const auto *source = static_cast<const char *>(mapped.pData) + p.first;
                for (UINT y = 0; y < p.rows; ++y)
                    std::memcpy(data.data() + p.offset + size_t(y) * p.pitch,
                                source + size_t(y) * mapped.RowPitch, p.pitch);
            }
            context->Unmap(p.staging.Get(), p.mip);
            need(valid, "texture_pitch");
            p.staging.Reset();
        }
        return true;
    }
};

struct capture_device {
    ID3D11DeviceContext *context;
    ComPtr<ID3D11Device> dev;
    ComPtr<ID3D11DeviceContext1> ctx1;
    writer out;
    packet_readbacks *pending;
    explicit capture_device(ID3D11DeviceContext *c, packet_readbacks *readbacks = nullptr)
        : context(c), pending(readbacks) {
        c->GetDevice(&dev); c->QueryInterface(IID_PPV_ARGS(&ctx1));
    }
    void buffer(ID3D11Buffer *b,UINT first=0,UINT length=UINT_MAX) {
        D3D11_BUFFER_DESC d{}; b->GetDesc(&d);
        need(d.ByteWidth && d.ByteWidth<=32*1024*1024 && !d.MiscFlags && first<d.ByteWidth,"unsupported_buffer");
        length=std::min(length,d.ByteWidth-first);
        need(length && length+4<=limit-out.data.size(),"buffer_budget");
        if (pending) pending->reserve_staging(d.ByteWidth);
        // D3D11 forbids partial copies of constant buffers. Slice the full copy after Map.
        D3D11_BUFFER_DESC staging{};
        staging.ByteWidth = d.ByteWidth;
        staging.Usage = D3D11_USAGE_STAGING;
        staging.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        ComPtr<ID3D11Buffer> temp;
        hr(dev->CreateBuffer(&staging, nullptr, &temp), "stage_buffer_failed");
        context->CopyResource(temp.Get(), b);
        if (pending) {
            out.put(uint32_t(length));
            pending->add(out, temp.Get(), 0, first, length, 1);
            return;
        }
        mapped_read m(context,temp.Get(),0);
        out.blob(static_cast<const char *>(m.map.pData)+first,length);
    }
    void texture(ID3D11Resource *resource,bool depth) {
        ComPtr<ID3D11Texture2D> src; hr(resource->QueryInterface(IID_PPV_ARGS(&src)),"requires_texture2d");
        D3D11_TEXTURE2D_DESC d{}; src->GetDesc(&d);
        need(!d.MiscFlags || d.MiscFlags==D3D11_RESOURCE_MISC_GENERATE_MIPS,"texture_misc_unsupported");
        if (depth) {
            need((d.Format==DXGI_FORMAT_D24_UNORM_S8_UINT || d.Format==DXGI_FORMAT_R24G8_TYPELESS) && d.MipLevels==1,"requires_d24s8_depth");
            d.Format=DXGI_FORMAT_R24G8_TYPELESS;
        }
        size_t total=sizeof(d);
        for (UINT i=0;i<d.MipLevels;++i) { UINT p,h; shape(d,i,p,h); total+=size_t(p)*h; }
        need(d.MipLevels && total<=limit-out.data.size(),"texture_budget");
        if (pending) pending->reserve_staging(total - sizeof(d));
        d.MiscFlags=d.BindFlags=0; d.Usage=D3D11_USAGE_STAGING; d.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
        ComPtr<ID3D11Texture2D> tmp; hr(dev->CreateTexture2D(&d,nullptr,&tmp),"stage_texture_failed");
        context->CopyResource(tmp.Get(),src.Get()); out.put(d);
        for (UINT i=0;i<d.MipLevels;++i) {
            UINT p,h; shape(d,i,p,h);
            if (pending) {
                pending->add(out, tmp.Get(), i, 0, p, h);
                continue;
            }
            mapped_read m(context,tmp.Get(),i);
            need(m.map.RowPitch>=p,"texture_pitch"); auto *bytes=static_cast<const char *>(m.map.pData);
            for (UINT y=0;y<h;++y) out.raw(bytes+size_t(y)*m.map.RowPitch,p);
        }
    }
    void shader(bool vs, const std::string &code) {
        out.blob(code.data(),code.size());
        ComPtr<ID3D11ShaderReflection> reflection;
        hr(D3DReflect(code.data(),code.size(),IID_PPV_ARGS(&reflection)),"shader_reflection_missing");
        D3D11_SHADER_DESC d{}; hr(reflection->GetDesc(&d),"shader_reflection_missing");
        for (UINT i=0;i<d.BoundResources;++i) {
            D3D11_SHADER_INPUT_BIND_DESC b{}; hr(reflection->GetResourceBindingDesc(i,&b),"shader_binding_missing");
            need(b.BindPoint<128 && b.BindCount<=128-b.BindPoint && b.BindCount,"shader_binding_range");
            for (UINT s=b.BindPoint;s<b.BindPoint+b.BindCount;++s) {
                if (b.Type==D3D_SIT_CBUFFER) {
                    need(s<14,"constant_slot");
                    ComPtr<ID3D11Buffer> cb; UINT first=0,count=4096;
                    if (ctx1) {
                        if (vs) ctx1->VSGetConstantBuffers1(s,1,&cb,&first,&count);
                        else ctx1->PSGetConstantBuffers1(s,1,&cb,&first,&count);
                    } else if (vs) context->VSGetConstantBuffers(s,1,&cb);
                    else context->PSGetConstantBuffers(s,1,&cb);
                    if (!cb) continue;
                    need(first<=UINT_MAX/16 && count && count<=4096,"unsupported_constant_range");
                    out.put(UINT(b.Type)); out.put(s); buffer(cb.Get(),first*16,count*16);
                } else if (b.Type==D3D_SIT_TEXTURE) {
                    ComPtr<ID3D11ShaderResourceView> view;
                    if (vs) context->VSGetShaderResources(s,1,&view); else context->PSGetShaderResources(s,1,&view);
                    if (!view) continue;
                    D3D11_SHADER_RESOURCE_VIEW_DESC vd{}; view->GetDesc(&vd);
                    need(vd.ViewDimension==D3D11_SRV_DIMENSION_TEXTURE2D,"requires_2d_srv");
                    ComPtr<ID3D11Resource> resource; view->GetResource(&resource);
                    need(context->GetResourceMinLOD(resource.Get())==0.f,"resource_min_lod");
                    out.put(UINT(b.Type)); out.put(s); out.put(vd); texture(resource.Get(),false);
                } else if (b.Type==D3D_SIT_SAMPLER) {
                    need(s<16,"sampler_slot"); ComPtr<ID3D11SamplerState> state;
                    if (vs) context->VSGetSamplers(s,1,&state); else context->PSGetSamplers(s,1,&state);
                    if (!state) continue;
                    D3D11_SAMPLER_DESC sd{}; state->GetDesc(&sd); out.put(UINT(b.Type)); out.put(s); out.put(sd);
                } else throw std::runtime_error("unsupported_shader_binding");
            }
        }
        out.put(end);
    }
    void encode(header h) {
        ComPtr<IDXGIDevice> dx; hr(dev.As(&dx),"adapter_identity");
        ComPtr<IDXGIAdapter> adapter; hr(dx->GetAdapter(&adapter),"adapter_identity");
        DXGI_ADAPTER_DESC ad{}; hr(adapter->GetDesc(&ad),"adapter_identity");
        h.adapter=ad.AdapterLuid; h.level=dev->GetFeatureLevel();
        ComPtr<ID3D11GeometryShader> gs; ComPtr<ID3D11HullShader> hs; ComPtr<ID3D11DomainShader> ds;
        context->GSGetShader(&gs,nullptr,nullptr); context->HSGetShader(&hs,nullptr,nullptr); context->DSGetShader(&ds,nullptr,nullptr);
        need(!gs && !hs && !ds,"requires_vs_ps_only");
        ComPtr<ID3D11BlendState> blend; context->OMGetBlendState(&blend,h.factors,&h.mask);
        ComPtr<ID3D11BlendState1> b1; need(blend && SUCCEEDED(blend.As(&b1)),"requires_blend1");
        b1->GetDesc1(&h.blend);
        ComPtr<ID3D11RasterizerState> raster; context->RSGetState(&raster);
        h.raster=CD3D11_RASTERIZER_DESC2(D3D11_DEFAULT);
        if (raster) {
            ComPtr<ID3D11RasterizerState2> r2; hr(raster.As(&r2),"requires_raster2"); r2->GetDesc2(&h.raster);
        }
        context->IAGetPrimitiveTopology(&h.topology);
        h.viewportCount=h.scissorCount=16;
        context->RSGetViewports(&h.viewportCount,h.viewports); context->RSGetScissorRects(&h.scissorCount,h.scissors);
        ComPtr<ID3D11DepthStencilState> state; context->OMGetDepthStencilState(&state,&h.stencilRef);
        h.depth=CD3D11_DEPTH_STENCIL_DESC(D3D11_DEFAULT); if (state) state->GetDesc(&h.depth);
        out.put(h);
        ComPtr<ID3D11VertexShader> vs; ComPtr<ID3D11PixelShader> ps;
        UINT vc=0, pc=0; context->VSGetShader(&vs,nullptr,&vc); context->PSGetShader(&ps,nullptr,&pc);
        need(!vc && !pc,"dynamic_classes_unsupported");
        ComPtr<ID3D11InputLayout> layout; context->IAGetInputLayout(&layout);
        auto elements=layout ? metadata(layout.Get(),"input_layout_metadata") : std::string{};
        const auto vertex_code = metadata(vs.Get(), "vs_bytecode", metadata_kind::vs);
        const auto pixel_code = metadata(ps.Get(), "ps_bytecode", metadata_kind::ps);
        out.blob(elements.data(),elements.size());
        shader(true,vertex_code); shader(false,pixel_code);
        const auto used = layout_vertex_slots(elements);
        for (UINT s=0;s<used.size();++s) {
            if (!used.test(s)) continue;
            ComPtr<ID3D11Buffer> vb; UINT stride,offset;
            context->IAGetVertexBuffers(s,1,&vb,&stride,&offset);
            if (vb) { out.put(s); out.put(stride); out.put(offset); buffer(vb.Get()); }
        }
        out.put(end);
        if (h.indexed) {
            ComPtr<ID3D11Buffer> ib; DXGI_FORMAT fmt{}; UINT offset=0;
            context->IAGetIndexBuffer(&ib,&fmt,&offset); need(ib!=nullptr,"index_buffer_missing");
            out.put(fmt); out.put(offset); buffer(ib.Get());
        }
        ComPtr<ID3D11DepthStencilView> depth; context->OMGetRenderTargets(0,nullptr,&depth);
        out.put(UINT(depth!=nullptr));
        if (depth) {
            D3D11_DEPTH_STENCIL_VIEW_DESC vd{}; depth->GetDesc(&vd);
            need(vd.Format==DXGI_FORMAT_D24_UNORM_S8_UINT && vd.ViewDimension==D3D11_DSV_DIMENSION_TEXTURE2D &&
                 vd.Texture2D.MipSlice==0,"requires_plain_d24s8_view");
            ComPtr<ID3D11Resource> r; depth->GetResource(&r); out.put(vd); texture(r.Get(),true);
        }
    }
};
}
