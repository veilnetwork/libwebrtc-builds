#include "modules/audio_device/win/wasapi_capture_budget.h"

#include <algorithm>
#include <cassert>
#include <cstdint>
#include <vector>

namespace {
using Budget = webrtc::WasapiCaptureBudget;
constexpr uint64_t kEpoch = 1'000'000;
constexpr uint32_t kRate = 48000;
constexpr uint32_t kBlock = kRate / 100;
constexpr uint64_t kStep = 100'000;

class Rig {
 public:
  Rig() : audio(kBlock, 12) { clock.Prepare(kBlock); }

  void push(uint64_t position, uint64_t stamp, uint64_t read, bool valid = true,
            bool discontinuity = false) {
    clock.Push(audio.data(), {position, stamp, kBlock, valid, discontinuity},
               read, [&](const int16_t* data, Budget::Packet packet) {
      assert(!packet.valid || packet.qpc <= read);
      assert(packet.frames > 0 && packet.frames <= clock.max_output_frames());
      pieces.push_back(packet);
      out.insert(out.end(), data, data + packet.frames);
    });
  }

  Budget clock{kRate, 1};
  std::vector<int16_t> audio;
  std::vector<int16_t> out;
  std::vector<Budget::Packet> pieces;
};
}  // namespace

int main() {
  Rig nominal;
  for (unsigned i = 0; i != 100; ++i) {
    const auto stamp = kEpoch + i * kStep;
    nominal.push(i * kBlock, stamp, stamp + 30'000);
    assert(nominal.pieces.back().qpc == stamp);
  }
  assert(nominal.out == std::vector<int16_t>(100 * kBlock, 12));
  assert(!nominal.clock.queued_frames());
  assert(!nominal.clock.compressed_frames() && !nominal.clock.filled_frames());

  Rig gap;
  gap.push(0, kEpoch, kEpoch + 30'000);
  gap.push(4 * kBlock, kEpoch + 4 * kStep, kEpoch + 4 * kStep + 30'000, true, true);
  std::vector<int16_t> expected(kBlock, 12);
  expected.insert(expected.end(), 3 * kBlock, 0);
  expected.insert(expected.end(), kBlock, 12);
  assert(gap.out == expected);
  assert(gap.clock.filled_frames() == 3 * kBlock);
  assert(!gap.clock.queued_frames());

  Rig no_gap_witness;
  no_gap_witness.push(0, kEpoch, kEpoch + 30'000);
  no_gap_witness.push(kBlock, kEpoch + 4 * kStep, kEpoch + 4 * kStep + 30'000, true, true);
  no_gap_witness.push(8 * kBlock, kEpoch + 5 * kStep, kEpoch + 5 * kStep + 30'000, false, true);
  assert(no_gap_witness.out == std::vector<int16_t>(3 * kBlock, 12));
  assert(!no_gap_witness.clock.filled_frames());

  Rig excess;
  for (unsigned i = 0; i != 50; ++i) {
    const auto stamp = kEpoch + i * 20'000;
    excess.push(i * kBlock, stamp, stamp + 30'000);
    assert(excess.out.size() <= (i * 2 + 20) * kRate / 1000);
    assert(excess.clock.queued_frames() <= 70 * kRate / 1000);
  }
  assert(excess.clock.compressed_frames() > 0);
  assert(!excess.clock.filled_frames());
  assert(std::all_of(excess.out.begin(), excess.out.end(),
                     [](int16_t sample) { return sample == 12; }));
  excess.push(0, kEpoch, kEpoch + 30'000);
  assert(!excess.clock.filled_frames());
}
