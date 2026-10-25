import UIKit

/// Programmatic app entry — no storyboard. Builds the window and installs
/// ViewController as the root. Uses the legacy (non-scene) lifecycle, so
/// Info.plist intentionally has no UIApplicationSceneManifest.
@main
final class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = ViewController()
        window.makeKeyAndVisible()
        self.window = window
        return true
    }
}
