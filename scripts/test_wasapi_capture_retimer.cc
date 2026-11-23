#include "modules/audio_device/win/wasapi_capture_retimer.h"

#include <cassert>
#include <cmath>
#include <cstdint>
#include <vector>

namespace {
using Retimer = webrtc::WasapiCaptureRetimer;
constexpr uint64_t kQpcStart = 3'000'000;
constexpr uint64_t kQpcPerSecond = 10'000'000;
constexpr double kPi = 3.14159265358979323846;

std::vector<int16_t> Wave(uint32_t frames, uint64_t qpc, uint64_t interval) {
  std::vector<int16_t> stereo(frames * 2);
  for (uint32_t i = 0; i < frames; ++i) {
    const double time = (qpc + interval * double(i) / frames) / kQpcPerSecond;
    stereo[2 * i] = static_cast<int16_t>(std::round(
        16000 * std::sin(2 * kPi * 4000 * time)));
    stereo[2 * i + 1] = -stereo[2 * i];
  }
  return stereo;
}

void BypassesNominalAndTransientCapture() {
  Retimer retimer(48000, 2);
  retimer.Prepare(480);
  uint64_t position = 0;
  uint64_t qpc = kQpcStart;
  for (unsigned i = 0; i < 200; ++i) {
    const uint64_t interval = i >= 20 && i < 45 ? 80000 : 100000;
    const auto input = Wave(480, qpc, interval);
    unsigned emitted = 0;
    retimer.Push(input.data(), {position, qpc, 480, true, false},
                 [&](const int16_t* samples, Retimer::Packet packet) {
                   ++emitted;
                   assert(samples == input.data());
                   assert(packet.position == position);
                   assert(packet.qpc == qpc);
                   assert(packet.frames == 480);
                 });
    assert(emitted == 1);
    position += 480;
    qpc += interval;
  }
}

void CorrectsBothDirections(uint32_t rate, uint64_t interval) {
  Retimer retimer(rate, 2);
  const uint32_t frames = rate / 100;
  retimer.Prepare(frames);
  uint64_t first_qpc = 0;
  uint64_t corrected_frames = 0;
  uint64_t qpc = kQpcStart;
  double signal = 0, noise = 0;
  for (uint32_t i = 0; i < 200; ++i) {
    const auto input = Wave(frames, qpc, interval);
    unsigned emitted = 0;
    retimer.Push(input.data(),
                 {uint64_t{i} * frames, qpc, frames, true, false},
                 [&](const int16_t* samples, Retimer::Packet packet) {
      ++emitted;
      assert(packet.valid);
      if (samples == input.data()) {
        assert(first_qpc == 0);
        return;
      }
      if (!first_qpc) {
        assert(packet.discontinuity);
        first_qpc = packet.qpc;
      }
      const double next_start_ticks =
          first_qpc + corrected_frames * double(kQpcPerSecond) / rate;
      assert(std::abs(next_start_ticks - packet.qpc) < 1.01);
      for (uint32_t j = 0; j < packet.frames; ++j) {
        const double time = double(first_qpc) / kQpcPerSecond +
                            double(corrected_frames + j) / rate;
        const double ideal = 16000 * std::sin(2 * kPi * 4000 * time);
        signal += ideal * ideal;
        noise += std::pow(samples[2 * j] - ideal, 2);
        assert(samples[2 * j] == -samples[2 * j + 1]);
      }
      corrected_frames += packet.frames;
    });
    assert(emitted <= 1);
    qpc += interval;
  }
  assert(first_qpc > kQpcStart + 6000000 - interval);
  assert(first_qpc < kQpcStart + 6000000 + 3 * interval);
  assert(corrected_frames ==
         ((qpc - interval - first_qpc) * rate + kQpcPerSecond - 1) /
             kQpcPerSecond);
  assert(10 * std::log10(signal / noise) > 28);
}

void FlushesGapWithoutInterpolatingAcrossIt() {
  Retimer retimer(48000, 1);
  retimer.Prepare(480);
  const std::vector<int16_t> before(480, -30000), after(480, 30000);
  for (uint32_t i = 0; i < 100; ++i) {
    retimer.Push(before.data(), {uint64_t{i} * 480, kQpcStart + i * 80000,
                                480, true, false},
                 [](const int16_t*, Retimer::Packet) {});
  }
  const uint64_t after_qpc = kQpcStart + 100 * 80000;
  unsigned emitted = 0;
  retimer.Push(after.data(), {49000, after_qpc, 480, true, false},
               [&](const int16_t* samples, Retimer::Packet packet) {
    ++emitted;
    assert(packet.frames == 480);
    for (uint32_t i = 0; i < packet.frames; ++i) assert(samples[i] == -30000);
  });
  assert(emitted == 1);
  retimer.Push(after.data(), {49480, after_qpc + 80000, 480, true, false},
               [&](const int16_t* samples, Retimer::Packet packet) {
    ++emitted;
    assert(packet.frames == 384 && packet.discontinuity);
    for (uint32_t i = 0; i < packet.frames; ++i) assert(samples[i] == 30000);
  });
  assert(emitted == 2);
  retimer.Push(after.data(), {49960, 0, 480, false, false},
               [&](const int16_t*, Retimer::Packet packet) {
    ++emitted;
    assert(packet.frames == 480);
    if (emitted == 4) assert(packet.discontinuity && !packet.valid);
  });
  assert(emitted == 4);
}

void RecoversNominalCaptureAfterSustainedDrift(bool short_gaps) {
  constexpr uint32_t kFrames = 480;
  Retimer retimer(48000, 1);
  retimer.Prepare(kFrames);
  const std::vector<int16_t> pcm(kFrames, 2000);
  uint64_t qpc = kQpcStart;
  bool corrected = false;
  unsigned tail_native = 0;
  unsigned releases = 0;
  uint64_t position = 0;
  for (uint32_t i = 0; i < 300; ++i) {
    const bool gap = short_gaps && i >= 120 && i % 12 == 0;
    if (gap) {
      position += kFrames;
      qpc += 100000;
    }
    unsigned emitted = 0;
    bool emitted_current = false;
    retimer.Push(pcm.data(), {position, qpc, kFrames, true, gap},
                 [&](const int16_t* samples, Retimer::Packet packet) {
      ++emitted;
      if (i < 120 && samples != pcm.data()) corrected = true;
      if (i >= 120 && samples == pcm.data()) {
        emitted_current = true;
        assert(packet.position == position && packet.qpc == qpc);
        if (emitted == 2) {
          assert(packet.discontinuity);
          ++releases;
        }
      }
      if (i >= 270) {
        assert(samples == pcm.data());
        assert(packet.position == position);
        assert(packet.qpc == qpc && packet.frames == kFrames);
        ++tail_native;
      }
    });
    assert(emitted <= 2);
    if (emitted == 2) assert(emitted_current);
    position += kFrames;
    qpc += i < 119 ? 80000 : i % 2 == 0 ? 90000 : 110000;
  }
  assert(corrected && tail_native == 30 && releases == 1);
}

}  // namespace

int main() {
  BypassesNominalAndTransientCapture();
  CorrectsBothDirections(48000, 78901);
  CorrectsBothDirections(44100, 121234);
  FlushesGapWithoutInterpolatingAcrossIt();
  RecoversNominalCaptureAfterSustainedDrift(false);
  RecoversNominalCaptureAfterSustainedDrift(true);
}
