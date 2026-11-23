#include "modules/audio_device/win/wasapi_render_retimer.h"

#include <cassert>

namespace {
using webrtc::WasapiRenderRetimer;
using webrtc::WasapiRenderDelayMs;
using webrtc::WasapiRenderClockPosition;
constexpr uint64_t kEpoch = 10'000'000;
constexpr double kTau = 6.283185307179586;

struct Tone {
  Tone(uint32_t sample_rate, double tone_hz)
      : rate(sample_rate), block(rate / 100), frequency(tone_hz) {}

  bool operator()(int16_t* pcm) {
    for (uint32_t i = 0; i < block; ++i, ++sample_offset) {
      pcm[2 * i] = static_cast<int16_t>(std::round(
          12000 * std::sin(kTau * frequency * sample_offset / rate)));
      pcm[2 * i + 1] = -pcm[2 * i];
    }
    return true;
  }
  uint32_t rate, block;
  double frequency;
  uint64_t sample_offset = 0;
};

void CorrectsStableDeviceClock(uint32_t rate, uint64_t elapsed_per_block,
                               double tone_hz) {
  Tone source(rate, tone_hz);
  WasapiRenderRetimer retimer(source.block, 2);
  std::vector<int16_t> output(2 * source.block);
  int16_t prior = 0;
  double sin_score = 0, cos_score = 0, energy = 0;
  size_t measured = 0;
  bool active = false;
  uint64_t active_at = 0;
  const uint64_t steps = 50'000'000 / elapsed_per_block;
  for (uint64_t i = 0; i < steps; ++i) {
    const uint64_t now = i * elapsed_per_block;
    retimer.Observe(i * source.block, kEpoch + now, rate);
    if (!retimer.active()) continue;
    if (!active) {
      active = true;
      active_at = now;
    }
    assert(retimer.Render(kEpoch + now, output.data(), source));
    assert(retimer.queued_frames() <= 6 * source.block);
    // Measure after the FIFO has converged. Reference actual hardware time
    // per frame, including non-integer report periods.
    if (now < 20'000'000) continue;
    for (uint32_t j = 0; j < source.block; ++j) {
      const double t = (now + double(elapsed_per_block) * j / source.block) /
                       10'000'000;
      const double angle = kTau * tone_hz * t;
      sin_score += output[j * 2] * std::sin(angle);
      cos_score += output[j * 2] * std::cos(angle);
      energy += static_cast<double>(output[j * 2]) * output[j * 2];
      assert(output[j * 2] == -output[j * 2 + 1]);
      assert(std::abs(int(output[j * 2]) - prior) < 13000 || !measured);
      prior = output[j * 2];
      ++measured;
    }
  }
  if (elapsed_per_block == 100'000) {
    assert(!active);
    return;
  }
  assert(active && active_at >= 2'000'000 && active_at <= 2'400'000);
  const uint64_t duration_after_gate =
      (steps - 1) * elapsed_per_block - active_at;
  assert(source.sample_offset / source.block == duration_after_gate / 100'000 + 1);
  assert(energy > 0 && measured > 40000);
  const double explained = 2 * (sin_score * sin_score + cos_score * cos_score) /
                           measured;
  const double coherence = explained / energy;
  assert(coherence > 0.96);
}

void IgnoresOneFastWindowAndInvalidTimestamps() {
  constexpr uint32_t kRate = 48000;
  WasapiRenderRetimer retimer(kRate / 100, 1);
  uint64_t position = 0;
  retimer.Observe(0, kEpoch, kRate);
  // One 100-ms observation at twice the advertised rate is insufficient.
  retimer.Observe(kRate / 5, kEpoch + 1'000'000, kRate);
  assert(!retimer.active());
  position = kRate / 5;
  // Restarted and duplicate positions cannot complete the prior window.
  retimer.Observe(position - 10, kEpoch + 1'500'000, kRate);
  retimer.Observe(position, kEpoch + 1'500'000, kRate);
  for (uint64_t i = 0; i < 20; ++i) {
    retimer.Observe(position + i * (kRate / 100),
                    kEpoch + 1'500'000 + i * 100'000, kRate);
    assert(!retimer.active());
  }
}
void ReanchorsSourceDeadlinesAfterLongOutputStall() {
  constexpr uint32_t kRate = 48000;
  constexpr uint32_t kBlock = kRate / 100;
  WasapiRenderRetimer retimer(kBlock, 1);
  retimer.Observe(0, kEpoch, kRate);
  retimer.Observe(9600, kEpoch + 1'000'000, kRate);
  retimer.Observe(19200, kEpoch + 2'000'000, kRate);
  assert(retimer.active());

  std::vector<int16_t> output(kBlock);
  unsigned pulls = 0;
  const auto pull = [&](int16_t* pcm) {
    ++pulls;
    std::fill_n(pcm, kBlock, int16_t{3000});
    return true;
  };
  const uint64_t start = kEpoch + 2'000'000;
  assert(retimer.Render(start, output.data(), pull));
  assert(pulls == 1);
  // Recovery from an ordinary delayed callback still allows two due blocks.
  assert(retimer.Render(start + 250'000, output.data(), pull));
  assert(pulls == 3);

  const uint64_t after_stall = start + 10'000'000;
  assert(retimer.Render(after_stall, output.data(), pull));
  assert(pulls == 4);
  assert(retimer.Render(after_stall + 50'000, output.data(), pull));
  assert(pulls == 4);
  assert(retimer.Render(after_stall + 100'000, output.data(), pull));
  assert(pulls == 5);
}

void DiscardsOldPcmAfterLongOutputStall() {
  constexpr uint32_t kRate = 48000;
  constexpr uint32_t kBlock = kRate / 100;
  constexpr uint64_t kDeviceBlockQpc = 50'000;
  WasapiRenderRetimer retimer(kBlock, 1);
  std::vector<int16_t> output(kBlock);
  int16_t level = 4000;
  const auto pull = [&](int16_t* pcm) {
    std::fill_n(pcm, kBlock, level);
    return true;
  };
  for (uint64_t block = 0; block < 240; ++block) {
    const uint64_t now = kEpoch + block * kDeviceBlockQpc;
    retimer.Observe(block * kBlock, now, kRate);
    if (retimer.active()) assert(retimer.Render(now, output.data(), pull));
  }
  assert(retimer.active());
  assert(retimer.queued_frames() > kBlock);
  assert(output.back() == 4000);

  // A short delay must leave the already-decoded audio in the queue.
  const uint64_t short_delay = kEpoch + 242 * kDeviceBlockQpc;
  level = -4000;
  assert(retimer.Render(short_delay, output.data(), pull));
  assert(output.front() == 4000);

  // Playback was stopped for 600 ms. The decoder can now supply fresh PCM.
  assert(retimer.Render(short_delay + 6'000'000, output.data(), pull));
  assert(std::all_of(output.begin(), output.end(),
                     [](int16_t sample) { return sample == -4000; }));
}

void UsesEndpointQueueWhenTheRenderClockCannotMeasureDelay() {
  constexpr uint32_t kRate = 48000;
  constexpr uint32_t kCapacity = 1440;
  constexpr uint32_t kPadding = 960;
  constexpr double kPipelineMs = 25;
  constexpr uint64_t kWritten = kRate * 30;
  constexpr double kResamplerFrames = 480;
  const auto delay = [&](std::optional<WasapiRenderClockPosition> clock) {
    return WasapiRenderDelayMs(kRate, kCapacity, kPadding, kPipelineMs,
                               kWritten, clock, kResamplerFrames);
  };

  // Thirty seconds of submitted audio do not describe the size of the queue.
  assert(delay(std::nullopt) == 55);
  assert(delay(WasapiRenderClockPosition{0, 0}) == 55);
  assert(delay(WasapiRenderClockPosition{0, kRate}) == 55);
  // A position ahead of the submitted stream must not wrap to uint16_t.
  assert(delay(WasapiRenderClockPosition{kWritten + 576, kRate}) == 55);
  // Keep a valid direct playback measurement and the software queue.
  assert(delay(WasapiRenderClockPosition{kWritten - 1680, kRate}) == 45);
  // The device clock can include a downstream delay beyond reported padding.
  assert(delay(WasapiRenderClockPosition{kWritten - 3360, kRate}) == 80);
  // A driver with a genuinely large endpoint buffer may exceed 500 ms.
  assert(WasapiRenderDelayMs(kRate, kRate, kRate / 2, 25, kWritten,
                             WasapiRenderClockPosition{kWritten - 28800, kRate},
                             0) == 600);
}

}  // namespace

int main() {
  ReanchorsSourceDeadlinesAfterLongOutputStall();
  DiscardsOldPcmAfterLongOutputStall();
  UsesEndpointQueueWhenTheRenderClockCannotMeasureDelay();
  IgnoresOneFastWindowAndInvalidTimestamps();
  CorrectsStableDeviceClock(48000, 100'000, 750);
  for (double tone : {750., 4000.}) {
    CorrectsStableDeviceClock(44100, 50'000, tone);
    CorrectsStableDeviceClock(48000, 167'000, tone);
  }
}
