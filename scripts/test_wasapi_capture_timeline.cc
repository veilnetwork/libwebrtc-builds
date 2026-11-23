#include <cassert>
#include <cstdint>
#include <limits>

#include "modules/audio_device/win/wasapi_capture_timeline.h"

using webrtc::WasapiCaptureTimeline;

void ContiguousPacketsKeepTheFirstFrameTimestamp() {
  WasapiCaptureTimeline capture(48000);
  capture.Append(0, 240, 1'000'000, 1'050'000, 0, true, false);
  capture.Retain(0, 240);
  // Delivery was late, but device positions did not skip.
  capture.Append(240, 720, 1'050'000, 1'600'000, 240, true, false);
  assert(capture.CaptureTimeNs(0) == 100'000'000);
  assert(capture.CaptureTimeNs(480) == 110'000'000);
  capture.Retain(960, 0);
  capture.Append(960, 480, 1'200'000, 1'650'000, 0, true, false);
  assert(capture.CaptureTimeNs(0) == 120'000'000);
}

void GapAcrossSameQpcIntervalDoesNotStampTheMixedBlock() {
  WasapiCaptureTimeline capture(48000);
  capture.Append(0, 96, 1'000'000, 1'130'000, 0, true, false);
  capture.Retain(0, 96);
  // A nominal 10-ms QPC step contains a real 384-frame hole.
  capture.Append(480, 960, 1'100'000, 1'330'000, 96, true, false);
  assert(!capture.CaptureTimeNs(0));
  assert(capture.CaptureTimeNs(480) == 118'000'000);
  capture.Retain(960, 96);
  capture.Append(1440, 384, 1'300'000, 1'410'000, 96, true, false);
  assert(capture.CaptureTimeNs(0) == 128'000'000);
}

void FlaggedDiscontinuityDoesNotStampOldSamples() {
  WasapiCaptureTimeline capture(48000);
  capture.Append(0, 240, 1'000'000, 1'080'000, 0, true, false);
  capture.Retain(0, 240);
  capture.Append(240, 960, 1'050'000, 1'330'000, 240, true, true);
  assert(!capture.CaptureTimeNs(0));
  assert(capture.CaptureTimeNs(480) == 110'000'000);
}

void MissingOrInvalidClockDoesNotReuseThePreviousTimestamp() {
  WasapiCaptureTimeline capture(44100);
  capture.Append(0, 441, 1'000'000, 1'110'000, 0, true, false);
  assert(capture.CaptureTimeNs(0) == 100'000'000);
  capture.Retain(441, 0);
  capture.Append(441, 441, 1'100'000, 1'210'000, 0, false, false);
  assert(!capture.CaptureTimeNs(0));
  capture.Append(882, 441, 1'320'000, 1'310'000, 0, true, false);
  assert(!capture.CaptureTimeNs(0));
  capture.Append(1323, 441, 1'300'000, 1'410'000, 0, true, false);
  assert(capture.CaptureTimeNs(0) == 130'000'000);
  capture.Append(1764, 441, 1'295'000, 1'510'000, 0, true, false);
  assert(!capture.CaptureTimeNs(0));
  capture.Append(2205, 441, 1'500'000, 1'610'000, 0, true, false);
  assert(capture.CaptureTimeNs(0) == 150'000'000);
}

void InvalidPositionsAndInterpolatedFutureTimesAreRejected() {
  WasapiCaptureTimeline capture(48000);
  capture.Append((std::numeric_limits<uint64_t>::max)(), 480,
                 1'000'000, 1'100'000, 0, true, false);
  assert(!capture.CaptureTimeNs(0));
  capture.Append(0, 480, 2'000'000, 2'010'000, 0, true, false);
  assert(capture.CaptureTimeNs(0) == 200'000'000);
  assert(!capture.CaptureTimeNs(240));
}

int main() {
  ContiguousPacketsKeepTheFirstFrameTimestamp();
  GapAcrossSameQpcIntervalDoesNotStampTheMixedBlock();
  FlaggedDiscontinuityDoesNotStampOldSamples();
  MissingOrInvalidClockDoesNotReuseThePreviousTimestamp();
  InvalidPositionsAndInterpolatedFutureTimesAreRejected();
}
