//! Compiles actual patched CLI methods against small dependency-graph substitutes.
//! Filesystem traversal, ignore matching and OS notifications use the real crates.
#![allow(dead_code)]
use notify::{event::ModifyKind, Event, EventKind, RecursiveMode, Watcher};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    time::Duration,
};
type NodeId = usize;

struct RootPath(PathBuf);
impl RootPath {
    fn as_std_path(&self) -> &Path {
        &self.0
    }
}
struct WorkspaceKrates(RootPath);
impl WorkspaceKrates {
    fn workspace_root(&self) -> &RootPath {
        &self.0
    }
}
struct Workspace {
    krates: WorkspaceKrates,
}
impl Workspace {
    // @DEFAULT_IGNORE_LIST@
    // @WORKSPACE_ROOT@
}
struct Build {
    root: PathBuf,
    crate_package: usize,
    public: Option<PathBuf>,
}
impl Build {
    fn crate_dir(&self) -> PathBuf {
        self.root.clone()
    }
    fn user_public_dir(&self) -> Option<PathBuf> {
        self.public.clone()
    }
}
struct AppBuilder {
    build: Build,
}
struct CachedFile {
    contents: String,
    most_recent: Option<String>,
    templates: (),
}
struct AppServer {
    client: AppBuilder,
    server: Option<AppBuilder>,
    workspace: Workspace,
    dependencies: HashMap<usize, Vec<PathBuf>>,
    file_map: HashMap<PathBuf, CachedFile>,
    watcher: Box<dyn Watcher>,
}
impl AppServer {
    fn client(&self) -> &AppBuilder {
        &self.client
    }
    fn local_dependencies(&self, package: usize) -> Vec<PathBuf> {
        self.dependencies.get(&package).cloned().unwrap_or_default()
    }
    // @ALL_WATCHED_CRATES@
    // @FILL_FILEMAP@
    // @WATCH_PATHS@
    // @WATCH_FILESYSTEM@
    fn admit_new_directories(&mut self, event: &Event) {
        // @ADMIT_NEW_DIRECTORIES@
    }
}
fn handle_notify_error(error: notify::Error) {
    panic!("watch registration failed: {error}");
}
// @SCOPED_ENTRIES@
// @METADATA_PROGRESS@

#[cfg(test)]
mod tests {
    use super::*;
    use notify::event::{CreateKind, RenameMode};
    use std::{
        fs,
        sync::{
            atomic::{AtomicUsize, Ordering},
            mpsc,
        },
        time::{Duration, Instant},
    };
    static NEXT: AtomicUsize = AtomicUsize::new(0);

    #[derive(Clone)]
    struct ProgressEvents(std::sync::Arc<std::sync::Mutex<Vec<(tracing::Level, u64)>>>);

    impl tracing::Subscriber for ProgressEvents {
        fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
            true
        }
        fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
            tracing::span::Id::from_u64(1)
        }
        fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
        fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
        fn enter(&self, _: &tracing::span::Id) {}
        fn exit(&self, _: &tracing::span::Id) {}
        fn event(&self, event: &tracing::Event<'_>) {
            struct Elapsed(Option<u64>);
            impl tracing::field::Visit for Elapsed {
                fn record_debug(&mut self, _: &tracing::field::Field, _: &dyn std::fmt::Debug) {}
                fn record_u64(&mut self, field: &tracing::field::Field, value: u64) {
                    if field.name() == "elapsed_seconds" {
                        self.0 = Some(value);
                    }
                }
            }
            let mut elapsed = Elapsed(None);
            event.record(&mut elapsed);
            if let Some(seconds) = elapsed.0 {
                self.0
                    .lock()
                    .unwrap()
                    .push((*event.metadata().level(), seconds));
            }
        }
    }

    #[tokio::test(start_paused = true)]
    async fn metadata_progress_is_info_every_ten_seconds_and_times_out_at_203() {
        let events = ProgressEvents(Default::default());
        let _subscriber = tracing::subscriber::set_default(events.clone());
        let task = tokio::spawn(metadata_progress());
        tokio::task::yield_now().await;
        tokio::time::advance(Duration::from_millis(999)).await;
        tokio::task::yield_now().await;
        assert!(events.0.lock().unwrap().is_empty());
        tokio::time::advance(Duration::from_millis(1)).await;
        tokio::task::yield_now().await;
        assert_eq!(*events.0.lock().unwrap(), vec![(tracing::Level::INFO, 1)]);
        for elapsed in (11..=201).step_by(10) {
            tokio::time::advance(Duration::from_secs(10)).await;
            tokio::task::yield_now().await;
            assert_eq!(events.0.lock().unwrap().last().unwrap().1, elapsed);
            assert!(!task.is_finished());
        }
        tokio::time::advance(Duration::from_secs(1)).await;
        tokio::task::yield_now().await;
        assert!(!task.is_finished());
        tokio::time::advance(Duration::from_secs(1)).await;
        task.await.unwrap();
        let recorded = events.0.lock().unwrap();
        assert_eq!(recorded.len(), 21);
        assert!(recorded
            .iter()
            .all(|(level, _)| *level == tracing::Level::INFO));
    }
    fn fixture() -> PathBuf {
        let root = PathBuf::from(std::env::var_os("ZETA_WATCH_FIXTURE_ROOT").unwrap())
            .join(format!("case-{}", NEXT.fetch_add(1, Ordering::SeqCst)));
        fs::create_dir_all(&root).unwrap();
        root
    }
    fn file(root: &Path, name: &str) -> PathBuf {
        let path = root.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, "pub fn original() {}\n").unwrap();
        path
    }
    fn server(
        root: &Path,
        dependencies: Vec<PathBuf>,
    ) -> (AppServer, mpsc::Receiver<notify::Result<Event>>) {
        let (tx, rx) = mpsc::channel();
        let watcher = notify::recommended_watcher(move |event| {
            tx.send(event).unwrap();
        })
        .unwrap();
        (
            AppServer {
                client: AppBuilder {
                    build: Build {
                        root: root.to_path_buf(),
                        crate_package: 0,
                        public: None,
                    },
                },
                server: None,
                workspace: Workspace {
                    krates: WorkspaceKrates(RootPath(root.to_path_buf())),
                },
                dependencies: HashMap::from([(0, dependencies)]),
                file_map: HashMap::new(),
                watcher: Box::new(watcher),
            },
            rx,
        )
    }
    fn expect_change(rx: &mpsc::Receiver<notify::Result<Event>>, expected: &Path) {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            let event = rx
                .recv_timeout(remaining)
                .unwrap_or_else(|_| panic!("no notification for {}", expected.display()))
                .unwrap();
            if event.paths.iter().any(|path| path == expected) {
                return;
            }
        }
    }
    #[test]
    fn single_crate_prunes_ignored_subtrees_before_descent() {
        let root = fixture();
        let app = root.join("app");
        let visible = file(&app, "src/lib.rs");
        for ignored in [
            ".git/forbidden/a.rs",
            "target/forbidden/a.rs",
            "node_modules/forbidden/a.rs",
            "generated-forbidden/a.rs",
            "src/nested-forbidden/a.rs",
        ] {
            file(&app, ignored);
        }
        file(&app, "assets/main.css");
        fs::write(
            app.join(".gitignore"),
            "/generated-forbidden/\nsrc/nested-forbidden/\n",
        )
        .unwrap();
        let (mut server, _rx) = server(&app, vec![]);
        server.fill_filemap_from_krate(app.clone());
        assert_eq!(server.file_map.len(), 1);
        assert!(server.file_map.contains_key(&visible));
        let paths = server.watch_paths(app.clone(), 0);
        assert_eq!(
            paths,
            vec![app.clone(), app.join("assets"), app.join("src")]
        );
    }
    #[test]
    fn workspace_and_cross_repository_roots_never_include_parent_or_sibling() {
        let root = fixture();
        let app = root.join("project/apps/owl");
        let local = root.join("shared");
        let inner = root.join("project/crates/core");
        file(&app, "src/main.rs");
        file(&local, "src/lib.rs");
        file(&inner, "src/lib.rs");
        file(&root, "forbidden-sibling/html/a.rs");
        let (mut server, _rx) = server(&app, vec![local.clone(), inner.clone(), local.clone()]);
        let roots = server.all_watched_crates();
        assert_eq!(roots.len(), 3);
        assert!(roots.contains(&local));
        assert!(roots.contains(&inner));
        assert!(!roots.contains(&root));
        assert!(!roots.contains(&root.join("project/crates")));
        for krate in roots {
            server.fill_filemap_from_krate(krate);
        }
        assert_eq!(server.file_map.len(), 3);
        assert!(server
            .file_map
            .keys()
            .all(|path| !path.to_string_lossy().contains("forbidden")));
        let paths = server.watch_paths(app, 0);
        assert!(paths.contains(&local));
        assert!(!paths.contains(&root));
    }
    #[test]
    fn fullstack_server_roots_and_external_assets_remain_admitted() {
        let root = fixture();
        let app = root.join("app");
        let api = root.join("api");
        let shared = root.join("shared");
        let public = root.join("public");
        for path in [&app, &api, &shared, &public] {
            file(path, "nested/item.rs");
        }
        let (mut server, _rx) = server(&app, vec![shared.clone()]);
        server.server = Some(AppBuilder {
            build: Build {
                root: api.clone(),
                crate_package: 1,
                public: None,
            },
        });
        server.dependencies.insert(1, vec![shared.clone()]);
        server.client.build.public = Some(public.clone());
        let roots = server.all_watched_crates();
        assert_eq!(roots.len(), 3);
        assert!(roots.contains(&api));
        assert!(roots.contains(&shared));
        let paths = server.watch_paths(app, 0);
        assert!(paths.contains(&public.join("nested")));
    }
    #[cfg(unix)]
    #[test]
    fn symlinks_do_not_escape_allowed_crates() {
        let root = fixture();
        let app = root.join("app");
        file(&app, "src/lib.rs");
        let outside = root.join("forbidden-outside");
        file(&outside, "nested/hidden.rs");
        std::os::unix::fs::symlink(&outside, app.join("escape")).unwrap();
        let (mut server, _rx) = server(&app, vec![]);
        server.fill_filemap_from_krate(app.clone());
        assert_eq!(server.file_map.len(), 1);
        assert!(!server
            .watch_paths(app.clone(), 0)
            .contains(&app.join("escape")));
    }
    #[test]
    fn native_watcher_observes_rust_css_local_dependencies_and_new_directories() {
        let root = fixture();
        let app = root.join("app");
        let shared = root.join("shared");
        let rust = file(&app, "src/main.rs");
        let css = file(&app, "assets/main.css");
        let dependency = file(&shared, "src/lib.rs");
        let (mut server, rx) = server(&app, vec![shared]);
        server.watch_filesystem();
        for path in [&rust, &css, &dependency] {
            fs::write(path, "changed\n").unwrap();
            expect_change(&rx, path);
        }
        let new_dir = app.join("src/new_module/nested");
        fs::create_dir_all(&new_dir).unwrap();
        server.admit_new_directories(
            &Event::new(EventKind::Create(CreateKind::Folder)).add_path(new_dir),
        );
        let new_file = file(&app, "src/new_module/nested/new.rs");
        expect_change(&rx, &new_file);
        let incoming = root.join("incoming");
        file(&incoming, "nested/moved.rs");
        let moved = app.join("src/moved_module");
        fs::rename(incoming, &moved).unwrap();
        server.admit_new_directories(
            &Event::new(EventKind::Modify(ModifyKind::Name(RenameMode::To)))
                .add_path(moved.clone()),
        );
        let moved_file = moved.join("nested/moved.rs");
        fs::write(&moved_file, "changed after move\n").unwrap();
        expect_change(&rx, &moved_file);
    }
}
