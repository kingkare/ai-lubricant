import UIKit

/// 单页验证界面：填写服务器地址/端口后点击「连接」，即可把远程服务器的流量
/// 转发到本机 iOS 服务（如 remoted/RSD 58783、testmanagerd），用于
/// USB-free、非同网段的可达性验证。界面尽量精简。
final class ViewController: UIViewController {

    private let proxy = TCPProxy()
    private let hostField = UITextField()
    private let portField = UITextField()
    private let hintField = UITextField()
    private let button = UIButton(type: .system)
    private let logView = UITextView()
    private var running = false
    private lazy var clock: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "HH:mm:ss"; return f
    }()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        setupUI()
        appendLog("设备代理就绪，填写服务器地址后点击「连接」。")
    }

    private func setupUI() {
        hostField.placeholder = "服务器地址"
        portField.placeholder = "服务器端口"
        hintField.placeholder = "目标主机提示（仅参考，实际由服务器下发）"
        portField.text = "9000"
        hintField.text = "127.0.0.1:58783"
        hostField.keyboardType = .URL
        portField.keyboardType = .numberPad
        hintField.isEnabled = false
        for f in [hostField, portField, hintField] {
            f.borderStyle = .roundedRect
            f.autocapitalizationType = .none
            f.autocorrectionType = .no
            f.font = .monospacedSystemFont(ofSize: 14, weight: .regular)
            f.heightAnchor.constraint(equalToConstant: 36).isActive = true
        }

        button.setTitle("连接", for: .normal)
        button.titleLabel?.font = .systemFont(ofSize: 18, weight: .semibold)
        button.addTarget(self, action: #selector(toggle), for: .touchUpInside)
        button.heightAnchor.constraint(equalToConstant: 44).isActive = true

        logView.isEditable = false
        logView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        logView.backgroundColor = .secondarySystemBackground
        logView.layer.cornerRadius = 6
        logView.textContainerInset = UIEdgeInsets(top: 8, left: 6, bottom: 8, right: 6)

        let stack = UIStackView(arrangedSubviews: [hostField, portField, hintField, button, logView])
        stack.axis = .vertical
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 12),
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16),
            stack.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -12),
        ])
    }

    @objc private func toggle() {
        view.endEditing(true)
        running ? disconnect() : connect()
    }

    private func connect() {
        let host = (hostField.text ?? "").trimmingCharacters(in: .whitespaces)
        guard !host.isEmpty,
              let port = UInt16((portField.text ?? "").trimmingCharacters(in: .whitespaces)) else {
            appendLog("请填写有效的服务器地址和端口。"); return
        }
        running = true
        button.setTitle("断开", for: .normal)
        hostField.isEnabled = false; portField.isEnabled = false
        appendLog("开始连接 \(host):\(port) …")
        proxy.start(serverHost: host, serverPort: port) { [weak self] line in
            DispatchQueue.main.async { self?.appendLog(line) }
        }
    }

    private func disconnect() {
        proxy.stop()
        running = false
        button.setTitle("连接", for: .normal)
        hostField.isEnabled = true; portField.isEnabled = true
        appendLog("已断开。")
    }

    private func appendLog(_ s: String) {
        logView.text += "[\(clock.string(from: Date()))] \(s)\n"
        logView.scrollRangeToVisible(NSRange(location: (logView.text as NSString).length, length: 0))
    }
}
