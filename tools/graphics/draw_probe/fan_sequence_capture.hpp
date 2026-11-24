// Included inside the probe's private namespace. The queue only contains staging
// resources, never live swapchain buffers, RTs, views or a game command list.
constexpr unsigned sequence_queue_limit = 5;
constexpr uint64_t sequence_pixel_limit = 8ull << 30;
constexpr uint64_t sequence_witness_limit = 4ull << 30;
constexpr uint64_t queued_witness_limit = 320ull << 20;
constexpr uint64_t sequence_free_reserve = 512ull << 20;

struct sequence_frame {
    unsigned index = 0, present = 0;
    int64_t qpc = 0;
    uint64_t bytes = 0, witness_bytes = 0, witness_staging_bytes = 0;
    std::wstring path;
    std::string header;
    std::vector<draw> events;
    decltype(pixel_captures) captures{};
};

struct sequence_entry {
    unsigned present;
    int64_t qpc;
    std::string file;
};

struct sequence_session {
    bool active = false, busy = false;
    std::string arm_token;
    unsigned target = 0, arm_present = 0, observed = 0, fault_present = 0;
    unsigned fault_rejected = 0, fault_dropped = 0;
    int64_t arm_qpc = 0, last_qpc = 0, frequency = 0;
    uint64_t bytes = 0, drain_started = 0, witness_reserved = 0, witness_published = 0;
    const char *fault = nullptr;
    std::deque<sequence_frame> queued;
    std::vector<sequence_entry> published;
};
sequence_session sequence_capture;

void begin_sequence(int64_t qpc) {
    auto &s = sequence_capture;
    s = {};
    LARGE_INTEGER frequency{};
    QueryPerformanceFrequency(&frequency);
    s.frequency = frequency.QuadPart;
    s.active = true;
    s.arm_token = request_token;
    sequence_witness_armed = fan_packet_enabled;
    s.target = requested_sequence_frames;
    s.arm_present = presents;
    s.arm_qpc = s.last_qpc = qpc;
    if (qpc <= 0 || s.frequency <= 0) s.fault = "performance_counter_unavailable";
}

void finish_sequence(std::unique_lock<std::mutex> &lock) {
    auto &s = sequence_capture;
    recording = false;
    std::ostringstream index;
    index << "{\"schema\":\"fan-native-sequence-v1\",\"status\":"
          << std::quoted(s.fault ? s.fault : "complete")
          << ",\"requestToken\":" << std::quoted(s.arm_token)
          << ",\"hostExecutable\":" << std::quoted(host_name()) << ",\"processId\":" << GetCurrentProcessId()
          << ",\"sessionShot\":" << session_shot << ",\"asset\":\"vfx/monster/gimmick3/eff/n4gb_b_g02c0a1.avfx\""
          << ",\"assetSha256\":" << std::quoted(fan_inventory::avfx_sha256)
          << ",\"armPresent\":" << s.arm_present << ",\"armQpc\":" << s.arm_qpc
          << ",\"qpcFrequency\":" << s.frequency << ",\"requestedFrames\":" << s.target
          << ",\"observedFrames\":" << s.observed << ",\"stagedPixelBytes\":" << s.bytes
          << ",\"stagedWitnessPacketBytes\":" << s.witness_published << ",\"witnessPacketByteBudget\":" << sequence_witness_limit
          << ",\"pixelByteBudget\":" << sequence_pixel_limit << ",\"maxQueuedFrames\":" << sequence_queue_limit
          << ",\"faultPresent\":" << (s.fault ? (s.fault_present ? s.fault_present : presents) : 0)
          << ",\"faultRejectedAnchorDraws\":" << s.fault_rejected << ",\"faultDroppedDraws\":" << s.fault_dropped
          << ",\"frames\":[";
    for (size_t i = 0; i < s.published.size(); ++i) {
        const auto &frame = s.published[i];
        if (i) index << ',';
        index << "{\"index\":" << i << ",\"present\":" << frame.present << ",\"qpc\":" << frame.qpc
              << ",\"report\":" << std::quoted(frame.file) << '}';
    }
    index << "]}\n";
    if (!publish_new(report_path, index.str()))
        log::message(log::level::error, "Draw probe could not publish the sequence index.");
    // Releasing native objects may re-enter resource destruction callbacks.
    std::deque<sequence_frame> retired;
    retired.swap(s.queued);
    decltype(pixel_captures) abandoned;
    discard_fan_frame(abandoned);
    s.active = s.busy = sequence_witness_armed = false;
    fan_active = false;
    immediate = nullptr;
    stop_request_watch();
    if (rearm_sidecar) advance_profile_shot();
    else done = true;
    lock.unlock();
}

// Called under gate while the observations still belong to this exact interval.
void snapshot_sequence_frame(sequence_frame &frame) {
    auto &s = sequence_capture;
    frame.path = report_path + L".frame-" + std::to_wstring(frame.index) + L".json";
    const auto anchors = select_fan_anchors();
    std::ostringstream header;
    header << std::boolalpha << "{\"api\":\"D3D11\",\"sdkApi\":" << RESHADE_API_VERSION
           << ",\"captureMode\":\"manual\",\"pixelCaptureMode\":\"before_after_rt0\",\"configurationSource\":"
           << std::quoted(configuration_source) << ",\"hostExecutable\":" << std::quoted(host_name())
           << ",\"processId\":" << GetCurrentProcessId() << ",\"sessionShot\":" << session_shot
           << ",\"present\":" << frame.present << ",\"armPresentThreadId\":" << arm_present_thread
           << ",\"sampledPresentThreadId\":" << GetCurrentThreadId()
           << ",\"drawCount\":" << draw_count << ",\"drawIndexedCount\":" << indexed_count
           << ",\"deferredDrawsObserved\":" << deferred
           << ",\"sequenceFrame\":{\"index\":" << frame.index << ",\"qpc\":" << frame.qpc
           << ",\"qpcFrequency\":" << s.frequency << "},\"fanCast\":{\"status\":\"captured\",\"qualification\":\"captured\""
           << ",\"asset\":\"vfx/monster/gimmick3/eff/n4gb_b_g02c0a1.avfx\""
           << ",\"assetSha256\":" << std::quoted(fan_inventory::avfx_sha256)
           << ",\"unreflectedDrawsWithFanTexturesLive\":" << fan_unreflected_draws
           << ",\"textureMatchedDraws\":" << fan_identified_draws << ",\"recognizedLiveTextures\":" << fan_textures.size()
           << ",\"rejectedAnchorDraws\":" << fan_rejected_draws << ",\"budgetDroppedDraws\":" << fan_budget_dropped
           << ",\"stagedCoverageDraws\":" << fan_staged_draws[0] << ",\"stagedLightningDraws\":" << fan_staged_draws[1]
           << ",\"stagedDecalAddDraws\":" << fan_staged_draws[2]
           << ",\"observationEntriesDropped\":" << fan_observations_dropped
           << ",\"anchors\":{\"coverageSequence\":" << (anchors.coverage ? anchors.coverage->sequence : 0)
           << ",\"lightningSequence\":" << (anchors.lightning ? anchors.lightning->sequence : 0)
           << ",\"shareTarget\":" << (anchors.coverage && anchors.lightning &&
                                    anchors.coverage->identity.texture.Get() == anchors.lightning->identity.texture.Get())
           << '}';
    write_fan_observations(header);
    header << '}';
    frame.header = header.str();
    frame.events.swap(rows);
    std::sort(frame.events.begin(), frame.events.end(), [](const auto &a, const auto &b) {
        return a.draw_ordinal < b.draw_ordinal;
    });
    pixel_captures.swap(frame.captures);
    draw_count = indexed_count = deferred = 0;
    fan_identified_draws = fan_rejected_draws = fan_budget_dropped = fan_observations_dropped = 0;
    fan_observations.clear();
    fan_unreflected_draws = 0;
    first_fan_stage_failure = nullptr;
    fan_staged_draws = {};
}

// Publication is strictly oldest-first. A busy plane remains staged for the
// next present; successful plane files are never written a second time.
const char *publish_sequence_frame(api::command_list *list, sequence_frame &frame) {
    if (!list) return "missing_present_context";
    auto *context = reinterpret_cast<ID3D11DeviceContext *>(list->get_native());
    if (!context) return "unsupported_present_context";
    {
        native_draw_scope scope(context, true);
        if (!scope.protected_context() || context->GetType() != D3D11_DEVICE_CONTEXT_IMMEDIATE)
            return "unsupported_present_context_threading";
        for (auto &capture : frame.captures) {
            if (!capture.before) continue;
            capture.readback_thread = GetCurrentThreadId();
            if (capture.packet_staging) {
                try {
                    if (!capture.packet_staging->collect(context, capture.packet_bytes)) return nullptr;
                } catch (const std::exception &error) {
                    capture.packet_status = error.what();
                    std::string{}.swap(capture.packet_bytes);
                }
                capture.packet_staging.reset();
            }
            const auto status = publish_pixels(context, capture, D3D11_MAP_FLAG_DO_NOT_WAIT, frame.path, true);
            if (!status) return nullptr;
            if (std::strcmp(status, "captured")) return status;
            capture.status = status;
            capture.before.Reset();
            capture.after.Reset();
        }
    }
    std::ostringstream out;
    const auto &terminal = frame.captures[terminal_index];
    out << std::boolalpha << frame.header << ",\"width\":" << terminal.target_width << ",\"height\":" << terminal.target_height;
    write_terminal(out, terminal);
    write_events(out, frame.events, frame.captures, true);
    return publish_new(frame.path, out.str()) ? "captured" : "frame_report_publish_failed";
}

void sequence_present(std::unique_lock<std::mutex> &lock, api::command_queue *queue,
                      api::swapchain *swapchain, int64_t qpc) {
    auto &s = sequence_capture;
    if (s.busy) {
        s.fault = "overlapping_present_callbacks";
        recording = false;
        return;
    }
    s.busy = true;
    if (read_request() == arm_request::cancel_fan) s.fault = "cancelled";
    if (!s.fault && recording) {
        if (s.arm_present > UINT_MAX - s.target || presents != s.arm_present + s.observed + 1)
            s.fault = "present_discontinuity";
        else if (qpc <= s.last_qpc) s.fault = "nonmonotonic_performance_counter";
        else if (active_fan_callbacks.load()) s.fault = "draw_callback_crossed_present";
        else if (fan_budget_dropped) s.fault = "fan_draw_budget_exhausted";
        else if (fan_rejected_draws) s.fault = first_fan_stage_failure ? first_fan_stage_failure : "fan_draw_rejected";
        else if (fan_observations_dropped) s.fault = "observation_budget_exhausted";
        else if (s.queued.size() >= sequence_queue_limit) s.fault = "staging_queue_exhausted";
        if (s.fault) {
            s.fault_present = presents;
            s.fault_rejected = fan_rejected_draws;
            s.fault_dropped = fan_budget_dropped;
        }
    }
    auto *list = immediate;
    if (!s.fault && recording) {
        sequence_frame frame;
        frame.index = s.observed++;
        frame.present = presents;
        frame.qpc = s.last_qpc = qpc;
        snapshot_sequence_frame(frame);
        if (s.observed == s.target) {
            recording = false;
            s.drain_started = GetTickCount64();
        }
        lock.unlock();
        auto &terminal = frame.captures[terminal_index];
        terminal.terminal = true;
        terminal.status = stage_terminal(list == queue->get_immediate_command_list() ? list : nullptr, swapchain, terminal);
        std::array<surface_identity, pixel_captures.size()> identities;
        detach_sampled_identities(identities, frame.captures);
        identities = {};
        const char *failure = nullptr;
        for (const auto &capture : frame.captures) {
            if (!capture.sequence && !capture.terminal) continue;
            if (!capture.status || std::strcmp(capture.status, "staged") || !capture.before) {
                failure = capture.status ? capture.status : "capture_crossed_present";
                break;
            }
            frame.bytes += uint64_t(capture.height) * capture.row_bytes * (capture.terminal ? 1 : 2);
        }
        ULARGE_INTEGER available{};
        const auto parent = std::filesystem::path(frame.path).parent_path();
        if (!failure && !GetDiskFreeSpaceExW(parent.c_str(), &available, nullptr, nullptr))
            failure = "disk_space_query_failed";
        lock.lock();
        if (!s.fault) s.fault = failure;
        if (!s.fault && frame.bytes > sequence_pixel_limit - s.bytes) s.fault = "sequence_pixel_budget_exhausted";
        uint64_t pending_bytes = frame.bytes;
        uint64_t pending_witness = 0, pending_witness_staging = 0;
        for (const auto &f : s.queued) {
            pending_bytes += f.bytes;
            pending_witness += f.witness_bytes;
            pending_witness_staging += f.witness_staging_bytes;
        }
        std::array<std::optional<dp::packet_readbacks>, pixel_captures.size()> rejected_staging;
        bool release_rejected = false;
        for (auto &cap : frame.captures) {
            if (!cap.alpha_witness || cap.packet_bytes.empty()) continue;
            const size_t size = cap.packet_bytes.size();
            const size_t staging = cap.packet_staging ? cap.packet_staging->staging_bytes : 0;
            if (size > sequence_witness_limit - s.witness_reserved ||
                size > queued_witness_limit - pending_witness ||
                staging > queued_witness_limit - pending_witness_staging) {
                cap.packet_status = "witness_packet_budget_exhausted";
                std::string{}.swap(cap.packet_bytes);
                if (cap.packet_staging) {
                    rejected_staging[&cap - frame.captures.data()].swap(cap.packet_staging);
                    release_rejected = true;
                }
                continue;
            }
            frame.witness_bytes += size;
            frame.witness_staging_bytes += staging;
            pending_witness += size;
            pending_witness_staging += staging;
            s.witness_reserved += size;
        }
        pending_bytes += pending_witness;
        if (!s.fault && available.QuadPart < pending_bytes + sequence_free_reserve) s.fault = "disk_space_reserve_reached";
        s.bytes += frame.bytes;
        s.queued.emplace_back(std::move(frame));
        if (release_rejected) {
            lock.unlock();
            rejected_staging = {};
            lock.lock();
        }
    }

    // Keep two complete intervals of slack before mapping on the protected
    // immediate context. The staging queue and disk use each have hard bounds.
    // Permit one extra retirement if a temporarily busy GPU left a backlog.
    const unsigned retire_limit = s.queued.size() >= 4 ? 2 : 1;
    for (unsigned n = 0; n < retire_limit && !s.fault && !s.queued.empty() &&
         (!recording || s.queued.size() >= 3); ++n) {
        auto &frame = s.queued.front();
        lock.unlock();
        const char *result = publish_sequence_frame(list, frame);
        lock.lock();
        if (!result) break;
        if (!s.fault) {
            if (std::strcmp(result, "captured") != 0) {
                s.fault = result;
                s.fault_present = frame.present;
            }
            else {
                for (const auto &cap : frame.captures) s.witness_published += cap.witness_file_bytes;
                s.published.push_back({frame.present, frame.qpc, std::filesystem::path(frame.path).filename().u8string()});
                s.queued.pop_front();
            }
        }
    }
    if (!s.fault && !recording && !s.queued.empty() && GetTickCount64() - s.drain_started >= 10000)
        s.fault = "sequence_readback_timed_out";
    if (s.fault || (!recording && s.queued.empty())) {
        finish_sequence(lock);
        return;
    }
    s.busy = false;
}
