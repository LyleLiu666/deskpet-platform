import Foundation
import Dispatch
import Darwin

// Stay alive as the app-bundled parent of the receiver; do not invoke a shell.
guard CommandLine.arguments.count >= 2 else {
    fputs("Missing receiver executable.\n", stderr)
    exit(64)
}
let receiver = Process()
receiver.executableURL = URL(fileURLWithPath: CommandLine.arguments[1])
receiver.arguments = Array(CommandLine.arguments.dropFirst(2))

// A caught signal is reset to its default disposition across exec, unlike an
// ignored signal; the Python child can therefore handle termination normally.
signal(SIGTERM, { _ in })
signal(SIGINT, { _ in })
let queue = DispatchQueue(label: "local.deskpet.receiver-signals")
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: queue)
let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: queue)
term.setEventHandler { if receiver.isRunning { kill(receiver.processIdentifier, SIGTERM) } }
interrupt.setEventHandler { if receiver.isRunning { kill(receiver.processIdentifier, SIGINT) } }
do {
    try receiver.run()
} catch {
    fputs("Unable to start receiver: \(error.localizedDescription)\n", stderr)
    exit(127)
}
term.resume()
interrupt.resume()
receiver.waitUntilExit()
let status = receiver.terminationStatus
exit(receiver.terminationReason == .uncaughtSignal ? 128 + status : status)
