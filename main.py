import os
import sys
import json
import queue
import platform
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

# GitPython 설치 확인
try:
    from git import Repo, GitCommandError, InvalidGitRepositoryError
except ImportError:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "오류",
        "GitPython이 설치되어 있지 않습니다.\n터미널에서 'pip install gitpython'을 실행해주세요."
    )
    _root.destroy()
    sys.exit(1)


# ==============================================================================
# 설정 관리 (Config)
# ==============================================================================

class Config:
    """애플리케이션 설정 관리 클래스"""

    # 파일 경로
    CONFIG_FILE = "config.json"
    SAVE_FILE = "repo_paths.txt"
    LOG_FILE = "log.txt"

    # 기본값
    DEFAULT_WINDOW_SIZE = "1200x800"
    DEFAULT_WINDOW_POSITION = ""
    DEFAULT_BATCH_SIZE = 8
    DEFAULT_MAX_DEPTH = 3

    # 로그 로테이션 설정
    LOG_MAX_BYTES = 1 * 1024 * 1024  # 1MB 초과 시 로테이션
    LOG_BACKUP_COUNT = 3             # 보관할 이전 로그 개수

    # UI 색상
    COLORS = {
        'bg': '#f8f9fa',
        'sidebar_bg': '#ffffff',
        'white': '#ffffff',
        'border': '#dee2e6',
        'primary': '#007bff',
        'primary_active': '#0056b3',
        'text': '#212529',
        'text_muted': '#6c757d',
        'success': '#28a745',
        'error': '#dc3545',
        'warning': '#ffc107',
        'info': '#17a2b8',
        'idle': '#6c757d',
        'statusbar_bg': '#e9ecef',
        'filter_inactive_bg': '#e9ecef',
        'tooltip_bg': '#2d2d2d',
        'tooltip_fg': '#ffffff',
    }

    # 로그 색상 (태그용)
    LOG_COLORS = {
        'SUCCESS': '#28a745',
        'ERROR': '#dc3545',
        'WARNING': '#fd7e14',
        'INFO': '#212529',
        'VERBOSE': '#6c757d',
    }

    # 상태 뱃지 색상
    BADGE_COLORS = {
        'idle': {'bg': '#6c757d', 'fg': '#ffffff', 'text': '대기'},
        'running': {'bg': '#007bff', 'fg': '#ffffff', 'text': '실행중'},
        'success': {'bg': '#28a745', 'fg': '#ffffff', 'text': '완료'},
        'error': {'bg': '#dc3545', 'fg': '#ffffff', 'text': '실패'},
        'uptodate': {'bg': '#17a2b8', 'fg': '#ffffff', 'text': '최신'},
        'warning': {'bg': '#ffc107', 'fg': '#000000', 'text': '경고'}
    }

    def __init__(self):
        self.window_size = self.DEFAULT_WINDOW_SIZE
        self.window_position = self.DEFAULT_WINDOW_POSITION
        self.batch_size = self.DEFAULT_BATCH_SIZE
        self.max_depth = self.DEFAULT_MAX_DEPTH
        self.stash_option = False
        self.load()

    def load(self):
        """설정 파일에서 설정 불러오기"""
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.window_size = data.get('window_size', self.DEFAULT_WINDOW_SIZE)
                self.window_position = data.get('window_position', self.DEFAULT_WINDOW_POSITION)
                self.batch_size = data.get('batch_size', self.DEFAULT_BATCH_SIZE)
                self.max_depth = data.get('max_depth', self.DEFAULT_MAX_DEPTH)
                self.stash_option = data.get('stash_option', False)
            except Exception as e:
                print(f"설정 불러오기 실패: {e}")

    def save(self):
        """설정 파일에 설정 저장"""
        try:
            data = {
                'window_size': self.window_size,
                'window_position': self.window_position,
                'batch_size': self.batch_size,
                'max_depth': self.max_depth,
                'stash_option': self.stash_option,
            }
            with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"설정 저장 실패: {e}")


# ==============================================================================
# Git 저장소 관리 (GitManager)
# ==============================================================================

class GitManager:
    """Git 저장소 관리 클래스"""

    @staticmethod
    def normalize_path(path):
        """경로 정규화 (중복 비교용 — normcase 적용으로 대소문자 통일)"""
        if not path:
            return path
        return os.path.normcase(os.path.abspath(os.path.normpath(path)))

    @staticmethod
    def normalize_path_preserve_case(path):
        """경로 정규화 (저장용 — 원본 대소문자 보존, 구분자/형태만 정규화)"""
        if not path:
            return path
        return os.path.abspath(os.path.normpath(path))

    @staticmethod
    def is_git_repo(path):
        """Git 작업 트리 저장소인지 빠르게 확인 (.git 유효성 검사, bare 제외)"""
        if not path or not os.path.isdir(path):
            return False

        git_path = os.path.join(path, '.git')
        if not os.path.exists(git_path):
            return False

        # worktree / submodule: .git 이 파일 (gitdir: ...)
        if os.path.isfile(git_path):
            try:
                with open(git_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if not content.startswith('gitdir:'):
                    return False
                gitdir = content[len('gitdir:'):].strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.normpath(os.path.join(path, gitdir))
                return os.path.isdir(gitdir) and os.path.isfile(os.path.join(gitdir, 'HEAD'))
            except Exception:
                return False

        # 일반 저장소: .git 디렉터리 + HEAD 필수 (빈/손상 .git 오탐 방지)
        if os.path.isdir(git_path):
            head = os.path.join(git_path, 'HEAD')
            if not os.path.isfile(head):
                return False
            # bare 저장소 형태가 .git 안에 있는 비정상 구조는 제외
            objects = os.path.join(git_path, 'objects')
            refs = os.path.join(git_path, 'refs')
            return os.path.isdir(objects) or os.path.isdir(refs) or os.path.isdir(
                os.path.join(git_path, 'worktrees')
            )

        return False

    @staticmethod
    def check_git_repo(path):
        """Git 저장소인지 확인 (.git 우선 검사 후 GitPython 검증)"""
        if not GitManager.is_git_repo(path):
            return False
        try:
            repo = Repo(path)
            # bare 저장소는 작업 트리 업데이트가 불가능하므로 제외
            if repo.bare:
                return False
            return True
        except InvalidGitRepositoryError:
            return False
        except Exception:
            return False

    @staticmethod
    def find_git_repos(root_path, max_depth=3):
        """지정된 경로 하위에서 모든 Git 저장소 찾기 (.git 우선 검사)

        max_depth는 검색할 최대 깊이(0부터 시작)를 의미합니다.
        max_depth=3이면 root(0) ~ 3단계 하위(총 4단계)까지 검색합니다.
        """
        git_repos = []

        def search_repos(current_path, depth):
            if depth > max_depth:
                return

            try:
                if GitManager.is_git_repo(current_path):
                    git_repos.append(GitManager.normalize_path(current_path))
                    return

                for item in os.listdir(current_path):
                    item_path = os.path.join(current_path, item)
                    if os.path.isdir(item_path) and not item.startswith('.'):
                        search_repos(item_path, depth + 1)

            except PermissionError:
                pass
            except Exception:
                pass

        search_repos(root_path, 0)
        return git_repos

    @staticmethod
    def get_branch_only(path):
        """브랜치 이름만 빠르게 조회 (HEAD 파일 직접 읽기)"""
        try:
            git_path = os.path.join(path, '.git')
            head_file = os.path.join(git_path, 'HEAD')

            if os.path.isfile(git_path):
                with open(git_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content.startswith('gitdir:'):
                        gitdir = content[len('gitdir:'):].strip()
                        if not os.path.isabs(gitdir):
                            head_file = os.path.join(path, gitdir, 'HEAD')
                        else:
                            head_file = os.path.join(gitdir, 'HEAD')

            with open(head_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()

            if content.startswith('ref: refs/heads/'):
                return content.replace('ref: refs/heads/', '')
            else:
                # Detached HEAD — 짧은 SHA 표시
                return f"detached:{content[:8]}" if content else '-'
        except Exception:
            return '-'

    @staticmethod
    def get_branch_name(repo):
        """브랜치 이름 조회. Detached HEAD면 None"""
        try:
            if repo.head.is_detached:
                return None
            return repo.active_branch.name
        except Exception:
            return None

    @staticmethod
    def get_remote(repo):
        """
        추적 브랜치 remote 우선, 없으면 origin, 그다음 첫 remote.
        remote가 없으면 None.
        """
        try:
            if not repo.head.is_detached:
                tracking = repo.active_branch.tracking_branch()
                if tracking is not None:
                    remote_name = tracking.remote_name
                    if remote_name in [r.name for r in repo.remotes]:
                        return repo.remotes[remote_name]
        except Exception:
            pass

        try:
            if 'origin' in [r.name for r in repo.remotes]:
                return repo.remotes.origin
            if repo.remotes:
                return repo.remotes[0]
        except Exception:
            pass

        return None

    @staticmethod
    def remote_ref_name(remote, branch):
        """remote/branch 형태의 ref 이름"""
        return f"{remote.name}/{branch}"

    @staticmethod
    def get_detailed_info(path):
        """저장소의 상세 정보 조회"""
        try:
            repo = Repo(path)
            if repo.bare:
                return {'success': False, 'error': 'bare 저장소는 지원하지 않습니다.'}

            head_commit = repo.head.commit
            branch = GitManager.get_branch_name(repo)
            if branch is None:
                branch = f"detached:{head_commit.hexsha[:8]}"

            return {
                'success': True,
                'branch': branch,
                'is_detached': repo.head.is_detached,
                'commit_sha': head_commit.hexsha[:8],
                'commit_message': head_commit.message.strip(),
                'author': f"{head_commit.author.name} <{head_commit.author.email}>",
                'commit_date': datetime.fromtimestamp(head_commit.committed_date).strftime('%Y-%m-%d %H:%M:%S'),
                'has_changes': repo.is_dirty(),
                'untracked_files': len(repo.untracked_files)
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @staticmethod
    def pull_latest(path):
        """현재 브랜치를 최신 버전으로 업데이트하고 상세 정보 반환"""
        try:
            repo = Repo(path)
            if repo.bare:
                return False, {'error': 'bare 저장소는 지원하지 않습니다.'}

            if repo.head.is_detached:
                return False, {
                    'error': 'Detached HEAD 상태입니다. 브랜치를 체크아웃한 뒤 다시 시도하세요.',
                    'is_detached': True
                }

            branch = repo.active_branch.name
            remote = GitManager.get_remote(repo)
            if remote is None:
                return False, {'error': 'remote가 설정되어 있지 않습니다.'}

            old_commit = repo.head.commit
            old_sha = old_commit.hexsha[:8]

            is_dirty = repo.is_dirty()
            untracked = repo.untracked_files

            remote.fetch()

            remote_branch = GitManager.remote_ref_name(remote, branch)
            ref_names = [ref.name for ref in repo.refs]
            if remote_branch in ref_names:
                commits_ahead = list(repo.iter_commits(f'{remote_branch}..{branch}'))
                remote_commit = repo.refs[remote_branch].commit

                if repo.head.commit == remote_commit:
                    return True, {
                        'already_updated': True,
                        'branch': branch,
                        'current_commit': old_sha,
                        'commit_message': old_commit.message.strip(),
                        'author': f"{old_commit.author.name} <{old_commit.author.email}>",
                        'commit_date': datetime.fromtimestamp(old_commit.committed_date).strftime('%Y-%m-%d %H:%M:%S'),
                        'is_dirty': is_dirty,
                        'untracked_files': len(untracked),
                        'commits_ahead': len(commits_ahead)
                    }

            remote.pull()

            new_commit = repo.head.commit
            new_sha = new_commit.hexsha[:8]

            if old_commit != new_commit:
                changed_files = []
                diff_index = old_commit.diff(new_commit)

                for diff_item in diff_index:
                    change_type = diff_item.change_type
                    file_path = diff_item.b_path if diff_item.b_path else diff_item.a_path

                    stats = {'additions': 0, 'deletions': 0}
                    # diff_item.diff 가 None(바이너리/신규 파일 등)이면 0/0 처리
                    raw_diff = getattr(diff_item, 'diff', None)
                    if raw_diff:
                        if isinstance(raw_diff, bytes):
                            try:
                                diff_text = raw_diff.decode('utf-8', errors='ignore')
                            except Exception:
                                diff_text = ''
                        else:
                            diff_text = str(raw_diff)

                        # 실제 +/- 라인만 카운트 (헤더/헝커 라인 제외)
                        for line in diff_text.splitlines():
                            if line.startswith('+++') or line.startswith('---'):
                                continue
                            if line.startswith('+'):
                                stats['additions'] += 1
                            elif line.startswith('-'):
                                stats['deletions'] += 1

                    changed_files.append({
                        'path': file_path,
                        'change_type': change_type,
                        'additions': stats['additions'],
                        'deletions': stats['deletions']
                    })

                new_commits = list(repo.iter_commits(f'{old_sha}..{new_sha}'))

                return True, {
                    'already_updated': False,
                    'branch': branch,
                    'old_commit': old_sha,
                    'new_commit': new_sha,
                    'new_commit_message': new_commit.message.strip(),
                    'new_commit_author': f"{new_commit.author.name} <{new_commit.author.email}>",
                    'new_commit_date': datetime.fromtimestamp(new_commit.committed_date).strftime('%Y-%m-%d %H:%M:%S'),
                    'changed_files': changed_files,
                    'total_files_changed': len(changed_files),
                    'commits_pulled': len(new_commits),
                    'commit_list': [
                        {
                            'sha': c.hexsha[:8],
                            'message': c.message.strip().split('\n')[0],
                            'author': c.author.name,
                            'date': datetime.fromtimestamp(c.committed_date).strftime('%Y-%m-%d %H:%M:%S')
                        } for c in new_commits
                    ]
                }
            else:
                return True, {
                    'already_updated': True,
                    'branch': branch,
                    'current_commit': old_sha
                }

        except GitCommandError as e:
            return False, {'error': f"Git 명령 실패: {str(e)}"}
        except Exception as e:
            return False, {'error': f"업데이트 실패: {str(e)}"}

    @staticmethod
    def stash_and_pull(path):
        """Stash → Pull → Stash Pop 로직 수행"""
        try:
            repo = Repo(path)
            if repo.bare:
                return False, {'error': 'bare 저장소는 지원하지 않습니다.'}

            if repo.head.is_detached:
                return False, {
                    'error': 'Detached HEAD 상태입니다. 브랜치를 체크아웃한 뒤 다시 시도하세요.',
                    'is_detached': True
                }

            branch = repo.active_branch.name
            remote = GitManager.get_remote(repo)
            if remote is None:
                return False, {'error': 'remote가 설정되어 있지 않습니다.'}

            old_commit = repo.head.commit
            old_sha = old_commit.hexsha[:8]

            has_changes = repo.is_dirty() or bool(repo.untracked_files)
            stash_created = False

            if has_changes:
                try:
                    repo.git.stash(
                        'push', '-u', '-m',
                        f'Auto-stash before update at {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
                    )
                    stash_created = True
                except GitCommandError as e:
                    return False, {'error': f"Stash 생성 실패: {str(e)}", 'stash_created': False}

            try:
                remote.fetch()

                remote_branch = GitManager.remote_ref_name(remote, branch)
                if remote_branch in [ref.name for ref in repo.refs]:
                    remote_commit = repo.refs[remote_branch].commit

                    if repo.head.commit == remote_commit:
                        if stash_created:
                            try:
                                repo.git.stash('pop')
                            except GitCommandError:
                                pass

                        return True, {
                            'already_updated': True,
                            'branch': branch,
                            'current_commit': old_sha,
                            'stash_created': stash_created,
                            'stash_popped': stash_created
                        }

                remote.pull()
                new_commit = repo.head.commit
                new_sha = new_commit.hexsha[:8]

                if stash_created:
                    try:
                        repo.git.stash('pop')
                        stash_popped = True
                    except GitCommandError as e:
                        return True, {
                            'already_updated': False,
                            'warning': f"Pull은 성공했지만 Stash Pop 실패 (충돌): {str(e)}",
                            'branch': branch,
                            'old_commit': old_sha,
                            'new_commit': new_sha,
                            'stash_created': True,
                            'stash_popped': False,
                            'has_conflict': True
                        }
                else:
                    stash_popped = False

                return True, {
                    'already_updated': False,
                    'branch': branch,
                    'old_commit': old_sha,
                    'new_commit': new_sha,
                    'new_commit_message': new_commit.message.strip(),
                    'new_commit_author': f"{new_commit.author.name} <{new_commit.author.email}>",
                    'new_commit_date': datetime.fromtimestamp(new_commit.committed_date).strftime('%Y-%m-%d %H:%M:%S'),
                    'stash_created': stash_created,
                    'stash_popped': stash_popped
                }

            except GitCommandError as e:
                if stash_created:
                    try:
                        repo.git.stash('pop')
                    except Exception:
                        pass
                return False, {'error': f"Pull 실패: {str(e)}", 'stash_created': stash_created}

        except GitCommandError as e:
            return False, {'error': f"Git 명령 실패: {str(e)}"}
        except Exception as e:
            return False, {'error': f"업데이트 실패: {str(e)}"}


# ==============================================================================
# UI 보조 컴포넌트 (ToolTip, StatusBadge)
# ==============================================================================

class ToolTip:
    """툴팁 클래스"""

    def __init__(self, widget):
        self.widget = widget
        self.tip_window = None

    def show_tip(self, text, x=None, y=None):
        if self.tip_window or not text:
            return

        if x is None or y is None:
            try:
                x, y, _, _ = self.widget.bbox("insert")
                x = x + self.widget.winfo_rootx() + 25
                y = y + self.widget.winfo_rooty() + 25
            except (tk.TclError, ValueError):
                x, y = self.widget.winfo_pointerxy()
                x += 25
                y += 25

        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes('-topmost', True)

        label = tk.Label(tw, text=text, justify=tk.LEFT,
                        background=Config.COLORS['tooltip_bg'],
                        foreground=Config.COLORS['tooltip_fg'],
                        relief=tk.SOLID, borderwidth=1,
                        font=("맑은 고딕", 9), padx=8, pady=5)
        label.pack()

    def hide_tip(self):
        tw = self.tip_window
        self.tip_window = None
        if tw:
            tw.destroy()


class StatusBadge:
    """상태 뱃지 컴포넌트"""

    def __init__(self, parent, status='idle'):
        colors = Config.BADGE_COLORS.get(status, Config.BADGE_COLORS['idle'])
        self.frame = tk.Frame(parent, bg=colors['bg'], padx=8, pady=2)
        self.label = tk.Label(
            self.frame,
            text=colors['text'],
            bg=colors['bg'],
            fg=colors['fg'],
            font=('맑은 고딕', 8, 'bold')
        )
        self.label.pack()

    def set_status(self, status):
        colors = Config.BADGE_COLORS.get(status, Config.BADGE_COLORS['idle'])
        self.frame.config(bg=colors['bg'])
        self.label.config(
            text=colors['text'],
            bg=colors['bg'],
            fg=colors['fg']
        )

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)


# ==============================================================================
# UI 레이아웃 Mixin (LayoutMixin)
# ==============================================================================

class LayoutMixin:
    """UI 레이아웃 및 스타일 Mixin"""

    def setup_styles(self):
        """ttk 스타일 설정"""
        style = ttk.Style()
        style.theme_use('clam')

        style.configure(
            'Action.TButton',
            font=('맑은 고딕', 10, 'bold'),
            padding=8
        )

        style.configure(
            'Secondary.TButton',
            font=('맑은 고딕', 9),
            padding=5
        )

        style.configure(
            'Card.TLabelframe',
            background='#ffffff',
            borderwidth=1,
            relief='solid'
        )
        style.configure(
            'Card.TLabelframe.Label',
            background='#ffffff',
            font=('맑은 고딕', 10, 'bold')
        )

        style.configure(
            'Custom.Treeview',
            rowheight=40,
            font=('맑은 고딕', 10)
        )
        style.configure(
            'Custom.Treeview.Heading',
            font=('맑은 고딕', 10, 'bold')
        )

    def create_widgets(self):
        """UI 컴포넌트 생성"""
        main_container = tk.Frame(self.root, bg=Config.COLORS['bg'])
        main_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.create_header(main_container)

        content_frame = tk.Frame(main_container, bg=Config.COLORS['bg'])
        content_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        content_frame.grid_columnconfigure(1, weight=1)
        content_frame.grid_rowconfigure(0, weight=1)

        self.create_repo_list_panel(content_frame)
        self.create_right_panel(content_frame)
        self.create_statusbar()

    def create_header(self, parent):
        """상단 헤더 영역 생성"""
        header = tk.Frame(parent, bg=Config.COLORS['white'], relief='solid', borderwidth=1)
        header.pack(fill=tk.X, pady=(0, 5))
        header.pack_propagate(False)
        header.config(height=70)

        title_frame = tk.Frame(header, bg=Config.COLORS['white'])
        title_frame.pack(side=tk.LEFT, padx=15, pady=10)

        tk.Label(
            title_frame,
            text="🔄 Git Branch Auto Updater",
            font=('맑은 고딕', 16, 'bold'),
            bg=Config.COLORS['white'],
            fg=Config.COLORS['text']
        ).pack(anchor='w')

        tk.Label(
            title_frame,
            text="여러 Git 저장소를 한 번에 업데이트하세요",
            font=('맑은 고딕', 9),
            bg=Config.COLORS['white'],
            fg=Config.COLORS['text_muted']
        ).pack(anchor='w')

        btn_frame = tk.Frame(header, bg=Config.COLORS['white'])
        btn_frame.pack(side=tk.RIGHT, padx=15, pady=15)

        ttk.Button(
            btn_frame,
            text="➕ 저장소 추가",
            command=self.add_repository,
            style='Secondary.TButton'
        ).pack(side=tk.LEFT, padx=2)

        ttk.Button(
            btn_frame,
            text="🔍 폴더 검색",
            command=self.add_folder_scan,
            style='Secondary.TButton'
        ).pack(side=tk.LEFT, padx=2)

        ttk.Separator(btn_frame, orient='vertical').pack(side=tk.LEFT, fill='y', padx=10)

        opt_frame = tk.Frame(btn_frame, bg=Config.COLORS['white'])
        opt_frame.pack(side=tk.LEFT, padx=5)

        ttk.Checkbutton(
            opt_frame,
            text="Stash 후 업데이트",
            variable=self.stash_option_var,
            command=self.on_stash_option_changed
        ).pack(side=tk.LEFT)

        batch_frame = tk.Frame(opt_frame, bg=Config.COLORS['white'])
        batch_frame.pack(side=tk.LEFT, padx=(15, 0))

        tk.Label(
            batch_frame,
            text="동시 처리:",
            bg=Config.COLORS['white'],
            font=('맑은 고딕', 9)
        ).pack(side=tk.LEFT)

        batch_spin = ttk.Spinbox(
            batch_frame,
            from_=1,
            to=16,
            width=4,
            textvariable=self.batch_size_var,
            command=self.on_batch_size_changed
        )
        batch_spin.pack(side=tk.LEFT, padx=3)

        ttk.Separator(btn_frame, orient='vertical').pack(side=tk.LEFT, fill='y', padx=10)

        self.update_btn = tk.Button(
            btn_frame,
            text="▶ 업데이트 실행",
            command=self.start_update_thread,
            bg=Config.COLORS['primary'],
            fg='white',
            font=('맑은 고딕', 10, 'bold'),
            padx=20,
            pady=6,
            cursor='hand2',
            relief='flat',
            activebackground=Config.COLORS['primary_active']
        )
        self.update_btn.pack(side=tk.LEFT, padx=5)

    def create_repo_list_panel(self, parent):
        """저장소 목록 패널 생성"""
        left_frame = tk.LabelFrame(
            parent,
            text=" 📁 저장소 목록 ",
            font=('맑은 고딕', 11, 'bold'),
            bg=Config.COLORS['white'],
            padx=10,
            pady=10
        )
        left_frame.grid(row=0, column=0, sticky='nsew', padx=(0, 5))
        left_frame.grid_propagate(False)
        left_frame.config(width=450)

        self.loading_frame = tk.Frame(left_frame, bg=Config.COLORS['white'])
        self.loading_label = tk.Label(
            self.loading_frame,
            text="⏳ 로딩 중...",
            bg=Config.COLORS['white'],
            fg=Config.COLORS['primary'],
            font=('맑은 고딕', 10, 'bold')
        )
        self.loading_label.pack(side=tk.LEFT)
        self.loading_progress = ttk.Progressbar(
            self.loading_frame,
            mode='indeterminate',
            length=100
        )
        self.loading_progress.pack(side=tk.LEFT, padx=10)

        search_frame = tk.Frame(left_frame, bg=Config.COLORS['white'])
        search_frame.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            search_frame,
            text="🔍",
            bg=Config.COLORS['white'],
            font=('맑은 고딕', 10)
        ).pack(side=tk.LEFT, padx=(0, 5))

        self.search_entry = ttk.Entry(
            search_frame,
            textvariable=self.search_var,
            font=('맑은 고딕', 10)
        )
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.search_entry.bind('<KeyRelease>', self.filter_repos)

        quick_actions = tk.Frame(left_frame, bg=Config.COLORS['white'])
        quick_actions.pack(fill=tk.X, pady=(0, 10))

        ttk.Button(
            quick_actions,
            text="🔄 새로고침",
            command=self.refresh_repo_list_async,
            width=12
        ).pack(side=tk.LEFT, padx=2)

        ttk.Button(
            quick_actions,
            text="❌ 선택 제거",
            command=self.remove_repository,
            width=12
        ).pack(side=tk.LEFT, padx=2)

        ttk.Button(
            quick_actions,
            text="🗑️ 전체 제거",
            command=self.remove_all_repositories,
            width=12
        ).pack(side=tk.LEFT, padx=2)

        tree_frame = tk.Frame(left_frame, bg=Config.COLORS['white'])
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ('status', 'name', 'branch', 'path')
        self.tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show='headings',
            style='Custom.Treeview',
            selectmode='extended'
        )

        self.tree.heading('status', text='상태')
        self.tree.heading('name', text='저장소명')
        self.tree.heading('branch', text='브랜치')
        self.tree.heading('path', text='경로')

        self.tree.column('status', width=70, anchor='center')
        self.tree.column('name', width=130, anchor='w')
        self.tree.column('branch', width=80, anchor='center')
        self.tree.column('path', width=150, anchor='w')

        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')

        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        self.tree.bind('<Double-1>', self.show_repo_info)
        self.tree.bind('<Button-3>', self.show_context_menu)
        self.tree.bind('<Motion>', self.on_tree_motion)

        self.tree_tooltip = ToolTip(self.tree)

        self.context_menu = tk.Menu(self.root, tearoff=0, bg=Config.COLORS['white'], font=('맑은 고딕', 9))
        self.context_menu.add_command(label="📋 상세 정보", command=self.show_repo_info)
        self.context_menu.add_command(label="📂 폴더 열기", command=self.open_repo_folder)
        self.context_menu.add_command(label="📋 경로 복사", command=self.copy_repo_path)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="❌ 목록에서 제거", command=self.remove_repository)

        self.create_status_summary(left_frame)

    def create_status_summary(self, parent):
        """상태 요약 카드 생성"""
        summary_frame = tk.LabelFrame(
            parent,
            text=" 📊 상태 요약 ",
            font=('맑은 고딕', 10, 'bold'),
            bg=Config.COLORS['bg'],
            padx=10,
            pady=8
        )
        summary_frame.pack(fill=tk.X, pady=(10, 0))

        cards_frame = tk.Frame(summary_frame, bg=Config.COLORS['bg'])
        cards_frame.pack(fill=tk.X)

        self.status_cards = {}
        statuses = [
            ('success', '✅ 완료', Config.COLORS['success']),
            ('uptodate', '💎 최신', Config.COLORS['info']),
            ('error', '❌ 실패', Config.COLORS['error']),
            ('idle', '⏸️ 대기', Config.COLORS['idle'])
        ]

        for i, (key, label, color) in enumerate(statuses):
            card = tk.Frame(cards_frame, bg=Config.COLORS['white'], relief='solid', borderwidth=1, padx=10, pady=8)
            card.grid(row=0, column=i, padx=3, pady=3, sticky='nsew')
            cards_frame.grid_columnconfigure(i, weight=1)

            tk.Label(
                card,
                text=label,
                bg=Config.COLORS['white'],
                fg=color,
                font=('맑은 고딕', 9, 'bold')
            ).pack()

            count_label = tk.Label(
                card,
                text='0',
                bg=Config.COLORS['white'],
                fg=color,
                font=('맑은 고딕', 16, 'bold')
            )
            count_label.pack()

            self.status_cards[key] = count_label

    def create_right_panel(self, parent):
        """우측 패널 생성 (로그 + 진행 상황)"""
        right_frame = tk.Frame(parent, bg=Config.COLORS['bg'])
        right_frame.grid(row=0, column=1, sticky='nsew')
        right_frame.grid_rowconfigure(1, weight=1)
        right_frame.grid_columnconfigure(0, weight=1)

        progress_frame = tk.LabelFrame(
            right_frame,
            text=" 📈 진행 상황 ",
            font=('맑은 고딕', 11, 'bold'),
            bg=Config.COLORS['white'],
            padx=10,
            pady=10
        )
        progress_frame.grid(row=0, column=0, sticky='ew', pady=(0, 5))

        self.progress = ttk.Progressbar(
            progress_frame,
            mode='determinate',
            length=400
        )
        self.progress.pack(fill=tk.X, pady=5)

        self.progress_label = tk.Label(
            progress_frame,
            text="준비 완료",
            bg=Config.COLORS['white'],
            font=('맑은 고딕', 10)
        )
        self.progress_label.pack()

        log_notebook = ttk.Notebook(right_frame)
        log_notebook.grid(row=1, column=0, sticky='nsew', pady=5)

        log_tab = tk.Frame(log_notebook, bg=Config.COLORS['white'])
        log_notebook.add(log_tab, text=" 📋 실행 로그 ")

        filter_frame = tk.Frame(log_tab, bg=Config.COLORS['white'])
        filter_frame.pack(fill=tk.X, padx=5, pady=5)

        self.log_filters = {}
        filter_levels = [
            ('ALL', '전체', Config.COLORS['primary']),
            ('SUCCESS', '성공', Config.COLORS['success']),
            ('ERROR', '오류', Config.COLORS['error']),
            ('INFO', '정보', Config.COLORS['text_muted'])
        ]

        for level, label, color in filter_levels:
            btn = tk.Button(
                filter_frame,
                text=label,
                bg=color if level == 'ALL' else Config.COLORS['filter_inactive_bg'],
                fg=Config.COLORS['white'] if level == 'ALL' else color,
                font=('맑은 고딕', 8),
                padx=10,
                relief='flat',
                command=lambda l=level: self.filter_log(l)
            )
            btn.pack(side=tk.LEFT, padx=2)
            self.log_filters[level] = btn

        self.current_log_filter = 'ALL'

        self.log_text = scrolledtext.ScrolledText(
            log_tab,
            wrap=tk.WORD,
            font=('Consolas', 9),
            state=tk.DISABLED,
            bg=Config.COLORS['bg'],
            padx=5,
            pady=5
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.setup_log_tags()

        result_tab = tk.Frame(log_notebook, bg=Config.COLORS['white'])
        log_notebook.add(result_tab, text=" 📊 상세 결과 ")

        self.result_text = scrolledtext.ScrolledText(
            result_tab,
            wrap=tk.WORD,
            font=('맑은 고딕', 9),
            state=tk.DISABLED,
            bg=Config.COLORS['bg'],
            padx=5,
            pady=5
        )
        self.result_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        log_actions = tk.Frame(right_frame, bg=Config.COLORS['bg'])
        log_actions.grid(row=2, column=0, sticky='ew', pady=5)

        ttk.Button(
            log_actions,
            text="📄 로그 파일 열기",
            command=self.open_log_file
        ).pack(side=tk.LEFT, padx=2)

        ttk.Button(
            log_actions,
            text="🗑️ 로그 지우기",
            command=self.clear_log
        ).pack(side=tk.LEFT, padx=2)

    def setup_log_tags(self):
        """로그 텍스트 태그 설정 (컬러링)"""
        for level, color in Config.LOG_COLORS.items():
            self.log_text.tag_config(level, foreground=color)

    def create_statusbar(self):
        """하단 상태바 생성"""
        status_frame = tk.Frame(self.root, bg=Config.COLORS['statusbar_bg'], relief='sunken', borderwidth=1)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self.status_var = tk.StringVar(value="준비 완료")

        self.status_label = tk.Label(
            status_frame,
            textvariable=self.status_var,
            bg=Config.COLORS['statusbar_bg'],
            font=('맑은 고딕', 9),
            anchor='w',
            padx=10,
            pady=5
        )
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        shortcut_label = tk.Label(
            status_frame,
            text="Ctrl+A: 전체 선택 | Delete: 제거 | F5: 새로고침",
            bg=Config.COLORS['statusbar_bg'],
            font=('맑은 고딕', 8),
            fg=Config.COLORS['text_muted'],
            padx=10
        )
        shortcut_label.pack(side=tk.RIGHT)


# ==============================================================================
# 스레드 안전 큐 처리 Mixin (QueueMixin)
# ==============================================================================

class QueueMixin:
    """스레드 안전 큐 처리 Mixin"""

    def process_queue(self):
        """UI 업데이트 큐 처리 (메인 스레드에서 호출)"""
        try:
            while True:
                try:
                    message = self.ui_queue.get_nowait()
                except queue.Empty:
                    break

                if not isinstance(message, dict):
                    continue

                msg_type = message.get('type')

                try:
                    if msg_type == 'LOG':
                        self._log_message_ui(message['text'], message['level'])
                    elif msg_type == 'TREE_UPDATE':
                        self._update_tree_item_status_ui(message['path'], message['status'], message.get('branch'))
                    elif msg_type == 'PROGRESS':
                        self._update_progress_ui(message['value'], message['text'])
                    elif msg_type == 'STATUS_SUMMARY':
                        self._update_status_summary_ui()
                    elif msg_type == 'BUTTON_STATE':
                        self._set_button_state_ui(message['state'], message.get('text'))
                    elif msg_type == 'POPUP':
                        self._show_popup_ui(message['title'], message['message'], message['msg_type'])
                    elif msg_type == 'LOADING_START':
                        self._show_loading_ui(True)
                    elif msg_type == 'LOADING_END':
                        self._show_loading_ui(False)
                    elif msg_type == 'TREE_INSERT':
                        self._tree_insert_ui(message['values'])
                    elif msg_type == 'TREE_CLEAR':
                        self._tree_clear_ui()
                    elif msg_type == 'STATUS_BAR':
                        self._set_status_bar_ui(message['text'])
                    elif msg_type == 'APPLY_REPOS':
                        self._apply_repos_ui(
                            message['results'],
                            clear_first=message.get('clear_first', False),
                            replace_all=message.get('replace_all', False),
                            save=message.get('save', False)
                        )
                except Exception as e:
                    try:
                        print(f"[QueueHandler] 메시지 처리 오류: {e}")
                    except Exception:
                        pass

        except Exception:
            pass
        finally:
            self.root.after(100, self.process_queue)

    def queue_log_message(self, message, level="INFO"):
        """로그 메시지를 큐에 추가 (스레드 안전)"""
        self.ui_queue.put({'type': 'LOG', 'text': message, 'level': level})

    def queue_tree_update(self, path, status, branch=None):
        """트리 업데이트를 큐에 추가 (스레드 안전)"""
        self.ui_queue.put({'type': 'TREE_UPDATE', 'path': path, 'status': status, 'branch': branch})

    def queue_progress(self, value, text):
        """진행률 업데이트를 큐에 추가 (스레드 안전)"""
        self.ui_queue.put({'type': 'PROGRESS', 'value': value, 'text': text})

    def queue_status_summary(self):
        """상태 요약 업데이트를 큐에 추가 (스레드 안전)"""
        self.ui_queue.put({'type': 'STATUS_SUMMARY'})

    def queue_button_state(self, state, text=None):
        """버튼 상태 변경을 큐에 추가 (스레드 안전)"""
        self.ui_queue.put({'type': 'BUTTON_STATE', 'state': state, 'text': text})

    def queue_popup(self, title, message, msg_type='info'):
        """팝업 표시를 큐에 추가 (스레드 안전)"""
        self.ui_queue.put({'type': 'POPUP', 'title': title, 'message': message, 'msg_type': msg_type})

    def queue_loading(self, show=True):
        """로딩 인디케이터 표시를 큐에 추가 (스레드 안전)"""
        self.ui_queue.put({'type': 'LOADING_START' if show else 'LOADING_END'})

    def queue_tree_insert(self, values):
        """트리 아이템 삽입을 큐에 추가 (스레드 안전)"""
        self.ui_queue.put({'type': 'TREE_INSERT', 'values': values})

    def queue_tree_clear(self):
        """트리 클리어를 큐에 추가 (스레드 안전)"""
        self.ui_queue.put({'type': 'TREE_CLEAR'})

    def queue_status_bar(self, text):
        """상태바 텍스트를 큐에 추가 (스레드 안전)"""
        self.ui_queue.put({'type': 'STATUS_BAR', 'text': text})

    def queue_apply_repos(self, results, clear_first=False, replace_all=False, save=False):
        """저장소 목록을 메인 스레드에서 일괄 반영 (스레드 안전)"""
        self.ui_queue.put({
            'type': 'APPLY_REPOS',
            'results': results,
            'clear_first': clear_first,
            'replace_all': replace_all,
            'save': save,
        })

    def _apply_repos_ui(self, results, clear_first=False, replace_all=False, save=False):
        """저장소 딕셔너리·트리 일괄 반영 (메인 스레드 전용)"""
        if replace_all or clear_first:
            self.repo_paths.clear()
            self.repo_status.clear()
            self.repo_branches.clear()
            self.tree.delete(*self.tree.get_children())

        for result in results:
            path = result['path']
            name = result['name']
            branch = result.get('branch', '-')
            self.repo_paths[path] = name
            self.repo_status[path] = 'idle'
            self.repo_branches[path] = branch
            self.tree.insert(
                '', 'end',
                values=(self.get_status_emoji('idle'), name, branch, path)
            )

        self._update_status_summary_ui()
        count = len(self.repo_paths)
        self.status_var.set(
            f"총 {count}개의 Git 저장소 등록됨" if count > 0 else "등록된 저장소 없음"
        )
        if save:
            self.save_paths()

    def _log_message_ui(self, message, level="INFO"):
        """UI에 로그 메시지 출력 (메인 스레드 전용)"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        emoji_map = {
            "INFO": "ℹ️",
            "SUCCESS": "✅",
            "WARNING": "⚠️",
            "ERROR": "❌",
            "VERBOSE": "📝"
        }

        line = f"{emoji_map.get(level, 'ℹ️')} {message}\n"
        if not hasattr(self, 'log_entries'):
            self.log_entries = []
        self.log_entries.append({'level': level, 'text': line})

        filter_level = getattr(self, 'current_log_filter', 'ALL')
        if self._log_matches_filter(level, filter_level):
            self.log_text.config(state=tk.NORMAL)
            start_pos = self.log_text.index(tk.END)
            self.log_text.insert(tk.END, line)
            end_pos = self.log_text.index(tk.END)
            self.log_text.tag_add(level, start_pos, end_pos)
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)

        try:
            self._rotate_log_if_needed()
            with open(Config.LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] [{level}] {message}\n")
        except Exception:
            pass

    def _rotate_log_if_needed(self):
        """로그 파일이 최대 크기를 초과하면 로테이션 수행 (메인 스레드 전용)"""
        try:
            if not os.path.exists(Config.LOG_FILE):
                return
            if os.path.getsize(Config.LOG_FILE) < Config.LOG_MAX_BYTES:
                return

            for i in range(Config.LOG_BACKUP_COUNT, 0, -1):
                src = f"{Config.LOG_FILE}.{i}"
                dst = f"{Config.LOG_FILE}.{i + 1}"
                if i == Config.LOG_BACKUP_COUNT:
                    if os.path.exists(src):
                        os.remove(src)
                    continue
                if os.path.exists(src):
                    os.rename(src, dst)

            os.rename(Config.LOG_FILE, f"{Config.LOG_FILE}.1")
        except Exception:
            pass

    def _log_matches_filter(self, level, filter_level):
        """로그 레벨이 현재 필터에 맞는지 여부"""
        if filter_level == 'ALL':
            return True
        if filter_level == 'SUCCESS':
            return level == 'SUCCESS'
        if filter_level == 'ERROR':
            return level == 'ERROR'
        if filter_level == 'INFO':
            return level in ('INFO', 'VERBOSE', 'WARNING')
        return level == filter_level

    def _render_log(self):
        """현재 필터에 맞게 로그 다시 그리기 (메인 스레드 전용)"""
        if not hasattr(self, 'log_entries'):
            self.log_entries = []

        filter_level = getattr(self, 'current_log_filter', 'ALL')

        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)

        for entry in self.log_entries:
            level = entry['level']
            if not self._log_matches_filter(level, filter_level):
                continue
            start_pos = self.log_text.index(tk.END)
            self.log_text.insert(tk.END, entry['text'])
            end_pos = self.log_text.index(tk.END)
            self.log_text.tag_add(level, start_pos, end_pos)

        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _update_tree_item_status_ui(self, path, status, branch=None):
        """UI에서 트리 아이템 상태 및 브랜치명 업데이트 (메인 스레드 전용)"""
        self.repo_status[path] = status
        if branch:
            self.repo_branches[path] = branch

        for item in self.tree.get_children():
            if self.tree.item(item, 'values')[3] == path:
                values = list(self.tree.item(item, 'values'))
                values[0] = self.get_status_emoji(status)
                values[2] = self.repo_branches.get(path, '-')
                self.tree.item(item, values=values)
                break

        self._update_status_summary_ui()

    def _update_progress_ui(self, value, text):
        """UI에서 진행률 업데이트 (메인 스레드 전용)"""
        self.progress['value'] = value
        self.progress_label.config(text=text)

    def _update_status_summary_ui(self):
        """UI에서 상태 요약 업데이트 (메인 스레드 전용)"""
        counts = {'success': 0, 'error': 0, 'uptodate': 0, 'idle': 0}

        for status in self.repo_status.values():
            if status in counts:
                counts[status] += 1
            else:
                counts['idle'] += 1

        for key, label in self.status_cards.items():
            label.config(text=str(counts.get(key, 0)))

    def _set_button_state_ui(self, state, text=None):
        """UI에서 버튼 상태 설정 (메인 스레드 전용)"""
        self.update_btn.config(state=state)
        if text:
            self.update_btn.config(text=text)
        if state == 'normal':
            self.update_btn.config(bg=Config.COLORS['primary'])

    def _set_status_bar_ui(self, text):
        """UI에서 상태바 텍스트 설정 (메인 스레드 전용)"""
        self.status_var.set(text)

    def _show_popup_ui(self, title, message, msg_type='info'):
        """UI에서 팝업 표시 (메인 스레드 전용)"""
        if msg_type == 'error':
            messagebox.showerror(title, message)
        elif msg_type == 'warning':
            messagebox.showwarning(title, message)
        else:
            messagebox.showinfo(title, message)

    def _show_loading_ui(self, show=True):
        """UI에서 로딩 인디케이터 표시 (메인 스레드 전용)"""
        if show:
            self.loading_frame.pack(fill=tk.X, pady=(0, 10), before=self.search_entry.master)
            self.loading_progress.start()
        else:
            self.loading_frame.pack_forget()
            self.loading_progress.stop()

    def _tree_insert_ui(self, values):
        """UI에서 트리 아이템 삽입 (메인 스레드 전용)"""
        self.tree.insert('', 'end', values=values)

    def _tree_clear_ui(self):
        """UI에서 트리 클리어 (메인 스레드 전용)"""
        self.tree.delete(*self.tree.get_children())

    def log_message(self, message, level="INFO"):
        """로그 메시지 출력 (스레드 안전)"""
        self.queue_log_message(message, level)

    def log_separator(self):
        """로그 구분선 (스레드 안전)"""
        sep = "=" * 80
        self.queue_log_message(sep, "INFO")

    def update_tree_item_status(self, path, status, branch=None):
        """트리 아이템 상태 업데이트 (스레드 안전)"""
        self.queue_tree_update(path, status, branch)

    def update_status_summary(self):
        """상태 요약 업데이트 (스레드 안전)"""
        self.queue_status_summary()

    def get_status_emoji(self, status):
        """상태에 따른 이모지 반환"""
        emoji_map = {
            'idle': '⏸️',
            'running': '🔄',
            'success': '✅',
            'error': '❌',
            'uptodate': '💎'
        }
        return emoji_map.get(status, '⏸️')

    def filter_log(self, level):
        """로그 필터링 (버튼 스타일 + 실제 목록 필터)"""
        colors = {
            'ALL': Config.COLORS['primary'],
            'SUCCESS': Config.COLORS['success'],
            'ERROR': Config.COLORS['error'],
            'INFO': Config.COLORS['text_muted']
        }

        for l, btn in self.log_filters.items():
            if l == level:
                btn.config(bg=colors[l], fg=Config.COLORS['white'])
            else:
                btn.config(bg=Config.COLORS['filter_inactive_bg'], fg=colors[l])

        self.current_log_filter = level
        self._render_log()

    def clear_log(self):
        """로그 지우기"""
        self.log_entries = []
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)


# ==============================================================================
# 저장소 관리 Mixin (RepoMixin)
# ==============================================================================

class RepoMixin:
    """저장소 관리 Mixin"""

    def on_stash_option_changed(self):
        """Stash 옵션 변경 시"""
        self.config.stash_option = self.stash_option_var.get()
        self.config.save()

    def on_batch_size_changed(self):
        """배치 크기 변경 시 (빈 값/비정상 값 방어)"""
        try:
            value = self.batch_size_var.get()
        except Exception:
            return
        if not value:
            return
        try:
            self.config.batch_size = int(value)
        except (ValueError, TypeError):
            return
        self.config.save()

    def select_all(self):
        """모든 저장소 선택"""
        self.tree.selection_set(self.tree.get_children())

    def filter_repos(self, event=None):
        """저장소 목록 필터링 (메모리 캐시 사용으로 성능 극대화)"""
        search_term = self.search_var.get().lower()

        self.tree.delete(*self.tree.get_children())

        for path, name in self.repo_paths.items():
            if search_term in name.lower() or search_term in path.lower():
                status = self.repo_status.get(path, 'idle')
                status_emoji = self.get_status_emoji(status)
                branch = self.repo_branches.get(path, '-')
                self.tree.insert('', 'end', values=(status_emoji, name, branch, path))

    def copy_repo_path(self):
        """저장소 경로 복사"""
        selection = self.tree.selection()
        if not selection:
            return

        item = selection[0]
        path = self.tree.item(item, 'values')[3]

        self.root.clipboard_clear()
        self.root.clipboard_append(path)
        self.log_message(f"경로 복사됨: {path}", level="INFO")

    def on_tree_motion(self, event):
        """마우스 이동 시 툴팁 표시"""
        item = self.tree.identify_row(event.y)
        if item:
            values = self.tree.item(item, 'values')
            if len(values) >= 4:
                full_path = values[3]
                x = event.x_root + 15
                y = event.y_root + 15
                self.tree_tooltip.show_tip(full_path, x, y)
        else:
            self.tree_tooltip.hide_tip()

    def show_context_menu(self, event):
        """우클릭 컨텍스트 메뉴"""
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def show_repo_info(self, event=None):
        """저장소 상세 정보 표시"""
        selection = self.tree.selection()
        if not selection:
            return

        item = selection[0]
        path = self.tree.item(item, 'values')[3]

        info = GitManager.get_detailed_info(path)

        if info['success']:
            detached_note = "\n⚠️ Detached HEAD 상태" if info.get('is_detached') else ""
            info_text = f"""
📁 저장소: {os.path.basename(path)}
📍 경로: {path}

🌿 브랜치: {info['branch']}{detached_note}
🔖 커밋: {info['commit_sha']}
💬 메시지: {info['commit_message'][:100]}
👤 작성자: {info['author']}
📅 날짜: {info['commit_date']}

{'⚠️ 로컬 변경사항 있음' if info['has_changes'] else '✅ 변경사항 없음'}
{'📝 추적되지 않은 파일: ' + str(info['untracked_files']) + '개' if info['untracked_files'] > 0 else ''}
            """
            messagebox.showinfo("저장소 정보", info_text)
        else:
            messagebox.showerror("오류", f"정보를 가져올 수 없습니다:\n{info.get('error', '알 수 없는 오류')}")

    def open_repo_folder(self):
        """저장소 폴더 열기"""
        selection = self.tree.selection()
        if not selection:
            return

        item = selection[0]
        path = self.tree.item(item, 'values')[3]

        try:
            if os.name == 'nt':
                os.startfile(path)
            elif platform.system() == 'Darwin':
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        except Exception as e:
            messagebox.showerror("오류", f"폴더를 열 수 없습니다:\n{str(e)}")

    def _path_already_added(self, folder_path):
        """정규화 경로 기준 중복 여부"""
        normalized = GitManager.normalize_path(folder_path)
        for existing in self.repo_paths:
            if GitManager.normalize_path(existing) == normalized:
                return True
        return False

    def add_repository(self):
        """저장소 추가"""
        folder_path = filedialog.askdirectory(title="Git 저장소 폴더 선택")
        if not folder_path:
            return

        folder_path = GitManager.normalize_path(folder_path)

        if not GitManager.check_git_repo(folder_path):
            messagebox.showerror("오류", "선택한 폴더는 유효한 Git 작업 트리 저장소가 아닙니다.")
            return

        if self._path_already_added(folder_path):
            messagebox.showinfo("알림", "이미 추가된 경로입니다.")
            return

        repo_name = os.path.basename(folder_path)
        self.repo_paths[folder_path] = repo_name
        self.repo_status[folder_path] = 'idle'

        branch = GitManager.get_branch_only(folder_path)
        self.repo_branches[folder_path] = branch

        self.tree.insert('', 'end', values=(self.get_status_emoji('idle'), repo_name, branch, folder_path))

        self.save_paths()
        self.update_status_summary()
        self.log_message(f"저장소 추가: {folder_path}", level="SUCCESS")
        self.update_status()

    def add_folder_scan(self):
        """폴더 검색하여 저장소 추가"""
        folder_path = filedialog.askdirectory(title="Git 저장소를 검색할 상위 폴더 선택")
        if not folder_path:
            return

        self.log_message(f"폴더 검색 시작: {folder_path}", level="INFO")
        max_depth = self.config.max_depth
        existing_normalized = {GitManager.normalize_path(p) for p in self.repo_paths}

        def scan_thread():
            try:
                found_repos = GitManager.find_git_repos(folder_path, max_depth=max_depth)
                local_additions = []

                for repo_path in found_repos:
                    norm = GitManager.normalize_path(repo_path)
                    if norm in existing_normalized:
                        continue
                    if not GitManager.check_git_repo(norm):
                        continue

                    repo_name = os.path.basename(norm)
                    branch = GitManager.get_branch_only(norm)
                    local_additions.append({
                        'path': norm,
                        'name': repo_name,
                        'branch': branch,
                    })
                    existing_normalized.add(norm)

                if local_additions:
                    self.queue_apply_repos(
                        local_additions, clear_first=False, replace_all=False, save=True
                    )

                msg = f"검색 완료: {len(found_repos)}개 발견, {len(local_additions)}개 추가"
                self.queue_log_message(msg, level="INFO")
                self.queue_popup("검색 완료", msg, 'info')

            except Exception as e:
                self.queue_log_message(f"검색 오류: {str(e)}", level="ERROR")

        threading.Thread(target=scan_thread, daemon=True).start()

    def remove_repository(self):
        """선택된 저장소 제거"""
        selection = self.tree.selection()
        if not selection:
            return

        for item in selection:
            path = self.tree.item(item, 'values')[3]
            if path in self.repo_paths:
                del self.repo_paths[path]
            if path in self.repo_status:
                del self.repo_status[path]
            if path in self.repo_branches:
                del self.repo_branches[path]

            self.tree.delete(item)

        self.save_paths()
        self.update_status_summary()
        self.update_status()

    def remove_all_repositories(self):
        """모든 저장소 제거"""
        if not self.repo_paths:
            return

        if not messagebox.askyesno("확인", "모든 저장소를 목록에서 제거하시겠습니까?"):
            return

        self.repo_paths.clear()
        self.repo_status.clear()
        self.repo_branches.clear()
        self.tree.delete(*self.tree.get_children())
        self.save_paths()
        self.update_status_summary()
        self.log_message("모든 저장소 목록 제거됨", level="INFO")
        self.update_status()

    def load_paths_async(self):
        """저장소 경로 비동기 불러오기 (UI Freeze 방지 + 병렬 처리)"""
        if not os.path.exists(Config.SAVE_FILE):
            return

        self._async_load_repos(
            clear_first=False,
            show_popup=False,
            action_label="불러오기"
        )

    def refresh_repo_list_async(self):
        """저장소 목록 비동기 새로고침 (UI Freeze 방지 + 병렬 처리)"""
        if self.is_loading:
            return

        if not os.path.exists(Config.SAVE_FILE):
            self.log_message(f"⚠️ {Config.SAVE_FILE} 파일이 존재하지 않습니다.", level="WARNING")
            messagebox.showwarning("알림", f"{Config.SAVE_FILE} 파일이 존재하지 않습니다.")
            return

        self._async_load_repos(
            clear_first=True,
            show_popup=True,
            action_label="새로고침"
        )

    def _async_load_repos(self, clear_first=False, show_popup=False, action_label="불러오기"):
        """load / refresh 공통 로직 (병렬 처리)"""
        self.is_loading = True
        self.queue_loading(True)
        self.queue_log_message(f"📂 저장소 목록 {action_label} 중...", level="INFO")

        batch_size = max(1, int(self.config.batch_size) if self.config.batch_size else 1)

        def load_thread():
            valid_count = 0
            invalid_count = 0
            results = []

            try:
                with open(Config.SAVE_FILE, 'r', encoding='utf-8') as f:
                    raw_paths = [line.strip() for line in f if line.strip()]

                paths = []
                seen = set()
                for p in raw_paths:
                    try:
                        norm = GitManager.normalize_path(p)
                    except Exception:
                        norm = p
                    if norm not in seen:
                        seen.add(norm)
                        paths.append(norm)

                valid_paths = []
                for path in paths:
                    if os.path.exists(path) and GitManager.is_git_repo(path):
                        valid_paths.append(path)
                    else:
                        invalid_count += 1
                        if clear_first:
                            self.queue_log_message(f"🚫 유효하지 않음: {path}", level="WARNING")

                if valid_paths:
                    with ThreadPoolExecutor(max_workers=batch_size) as executor:
                        futures = {
                            executor.submit(self._load_single_repo, path): path
                            for path in valid_paths
                        }

                        completed = 0
                        for future in as_completed(futures):
                            try:
                                result = future.result()
                                if result:
                                    results.append(result)
                                    valid_count += 1

                                completed += 1
                                progress = int(completed / len(valid_paths) * 100)
                                self.queue_progress(
                                    progress,
                                    f"{action_label} 중... {completed}/{len(valid_paths)}"
                                )
                            except Exception as e:
                                self.queue_log_message(f"저장소 로드 오류: {str(e)}", level="ERROR")
                else:
                    self.queue_progress(100, f"{action_label} 완료")

                path_order = {path: idx for idx, path in enumerate(paths)}
                results.sort(key=lambda x: path_order.get(x['path'], 9999))

                self.queue_apply_repos(
                    results,
                    clear_first=clear_first,
                    replace_all=clear_first
                )

                result_msg = f"{action_label} 완료: {valid_count}개 로드"
                if invalid_count > 0:
                    result_msg += f", {invalid_count}개 무시됨"

                self.queue_log_message(result_msg, level="INFO")
                if show_popup:
                    self.queue_popup(f"{action_label} 완료", result_msg, 'info')

            except Exception as e:
                self.queue_log_message(f"파일 읽기 오류: {str(e)}", level="ERROR")
                if show_popup:
                    self.queue_popup("오류", f"파일 읽기 실패:\n{str(e)}", 'error')
            finally:
                self.is_loading = False
                self.queue_loading(False)
                self.queue_progress(0, "준비 완료")

        threading.Thread(target=load_thread, daemon=True).start()

    def _load_single_repo(self, path):
        """단일 저장소 정보 로드 (병렬 처리용)"""
        try:
            if not GitManager.is_git_repo(path):
                return None

            repo_name = os.path.basename(path)
            branch = GitManager.get_branch_only(path)

            return {
                'path': path,
                'name': repo_name,
                'branch': branch
            }
        except Exception:
            return None

    def update_status(self):
        """상태바 업데이트"""
        count = len(self.repo_paths)
        self.status_var.set(f"총 {count}개의 Git 저장소 등록됨" if count > 0 else "등록된 저장소 없음")

    def save_paths(self):
        """저장소 경로 저장 (대소문자 보존 정규화)"""
        try:
            with open(Config.SAVE_FILE, 'w', encoding='utf-8') as f:
                for path in self.repo_paths.keys():
                    f.write(GitManager.normalize_path_preserve_case(path) + "\n")
        except Exception as e:
            self.log_message(f"경로 저장 실패: {str(e)}", level="ERROR")


# ==============================================================================
# 일괄 업데이트 Mixin (UpdaterMixin)
# ==============================================================================

class UpdaterMixin:
    """일괄 업데이트 Mixin"""

    def start_update_thread(self):
        """업데이트 스레드 시작"""
        if not self.repo_paths:
            messagebox.showinfo("알림", "업데이트할 저장소가 없습니다.")
            return

        if self.is_updating:
            messagebox.showwarning("경고", "이미 업데이트가 진행 중입니다.")
            return

        stash_enabled = self.stash_option_var.get()
        batch_size = self.batch_size_var.get()

        self.update_btn.config(state='disabled', text='⏳ 실행 중...')

        opt_msg = f"(동시 처리: {batch_size}개"
        if stash_enabled:
            opt_msg += ", Stash 모드: ON"
        opt_msg += ")"

        self.log_message(f"업데이트 시작 {opt_msg}", level="INFO")

        for path in self.repo_paths:
            self.repo_status[path] = 'idle'
            self.update_tree_item_status(path, 'idle')

        thread = threading.Thread(
            target=self.update_all_repos,
            args=(stash_enabled, batch_size),
            daemon=True
        )
        thread.start()

    def update_single_repo(self, path, idx, total, stash_enabled):
        """단일 저장소 업데이트 (옵션은 인자로 전달 — 스레드 안전)"""
        self.queue_log_message(f"\n[{idx}/{total}] 처리 중: {os.path.basename(path)}", level="INFO")
        self.update_tree_item_status(path, 'running')

        if not GitManager.check_git_repo(path):
            self.queue_log_message("  ⚠️ Git 저장소가 아님", level="WARNING")
            self.update_tree_item_status(path, 'error')
            return ('fail', path, None)

        info = GitManager.get_detailed_info(path)
        if info['success']:
            self.queue_log_message(f"  브랜치: {info['branch']} ({info['commit_sha']})", level="VERBOSE")
            if info.get('is_detached'):
                self.queue_log_message("  ⚠️ Detached HEAD — 업데이트 건너뜀", level="WARNING")
                self.update_tree_item_status(path, 'error', info['branch'])
                return ('fail', path, {'error': 'Detached HEAD', 'is_detached': True})
            if info['has_changes']:
                self.queue_log_message("  ⚠️ 로컬 변경사항 있음", level="WARNING")
        elif not info['success']:
            pass

        if stash_enabled:
            success, result = GitManager.stash_and_pull(path)

            if success:
                branch = result.get('branch')
                if result.get('already_updated'):
                    self.queue_log_message("  ✅ 이미 최신 버전", level="SUCCESS")
                    self.update_tree_item_status(path, 'uptodate', branch)
                    return ('already_updated', path, result)
                else:
                    self.queue_log_message(f"  ✅ 업데이트 완료 ({result['new_commit']})", level="SUCCESS")
                    if result.get('stash_created'):
                        if result.get('stash_popped'):
                            self.queue_log_message("     Stash 적용됨", level="VERBOSE")
                        elif result.get('has_conflict'):
                            self.queue_log_message("     ⚠️ Stash Pop 충돌 - 수동 해결 필요", level="WARNING")
                    self.update_tree_item_status(path, 'success', branch)
                    return ('success', path, result)
            else:
                error_msg = result.get('error', '알 수 없는 오류')
                self.queue_log_message(f"  ❌ 실패: {error_msg}", level="ERROR")
                self.update_tree_item_status(path, 'error')
                return ('fail', path, result)
        else:
            success, result = GitManager.pull_latest(path)

            if success:
                branch = result.get('branch')
                if result.get('already_updated'):
                    self.queue_log_message("  ✅ 이미 최신 버전", level="SUCCESS")
                    self.update_tree_item_status(path, 'uptodate', branch)
                    return ('already_updated', path, result)
                else:
                    self.queue_log_message(f"  ✅ 업데이트 완료 ({result['new_commit']})", level="SUCCESS")
                    if result.get('total_files_changed', 0) > 0:
                        self.queue_log_message(f"     변경: {result['total_files_changed']}개 파일", level="VERBOSE")
                    self.update_tree_item_status(path, 'success', branch)
                    return ('success', path, result)
            else:
                error_msg = result.get('error', '알 수 없는 오류')
                self.queue_log_message(f"  ❌ 실패: {error_msg}", level="ERROR")

                self.update_tree_item_status(path, 'error')
                return ('fail', path, result)

    def update_all_repos(self, stash_enabled, batch_size):
        """모든 저장소 업데이트 (옵션·배치 크기는 메인 스레드에서 전달)"""
        self.is_updating = True
        self.log_separator()
        self.queue_log_message("🚀 일괄 업데이트 시작", level="INFO")
        self.log_separator()

        self.queue_status_bar("업데이트 진행 중...")

        success_count = 0
        fail_count = 0
        already_updated_count = 0

        updated_names = []
        failed_names = []

        repo_paths_snapshot = dict(self.repo_paths)
        paths = list(repo_paths_snapshot.keys())
        total = len(paths)

        self.queue_progress(0, f"0/{total} 완료")

        workers = max(1, int(batch_size) if batch_size else 1)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self.update_single_repo, path, idx, total, stash_enabled
                ): path
                for idx, path in enumerate(paths, 1)
            }

            completed = 0
            for future in as_completed(futures):
                try:
                    status, repo_path, result = future.result()

                    repo_name = repo_paths_snapshot.get(repo_path, os.path.basename(repo_path))

                    if status == 'success':
                        success_count += 1
                        updated_names.append(repo_name)
                    elif status == 'already_updated':
                        already_updated_count += 1
                    else:
                        fail_count += 1
                        failed_names.append(repo_name)

                    completed += 1
                    progress_text = (
                        f"진행: {completed}/{total} "
                        f"(성공:{success_count}, 최신:{already_updated_count}, 실패:{fail_count})"
                    )
                    self.queue_progress(int(completed / total * 100), progress_text)

                except Exception as e:
                    self.queue_log_message(f"시스템 오류: {str(e)}", level="ERROR")
                    fail_count += 1
                    completed += 1

        self.log_separator()

        summary_msg = f"✨ 완료 - 성공:{success_count} | 최신:{already_updated_count} | 실패:{fail_count}"
        self.queue_log_message(summary_msg, level="INFO")

        if updated_names:
            self.queue_log_message(f"📌 [업데이트됨]: {', '.join(updated_names)}", level="SUCCESS")

        if failed_names:
            self.queue_log_message(f"📌 [실패함]: {', '.join(failed_names)}", level="ERROR")

        self.log_separator()

        self.queue_status_bar(
            f"완료 (성공:{success_count}, 최신:{already_updated_count}, 실패:{fail_count})"
        )
        self.is_updating = False

        self.queue_button_state('normal', '▶ 업데이트 실행')

        def show_result_popup():
            result_text = ""

            if updated_names:
                result_text += f"✅ 업데이트 성공 ({len(updated_names)}개):\n"
                result_text += ", ".join(updated_names) + "\n\n"

            if failed_names:
                result_text += f"❌ 업데이트 실패 ({len(failed_names)}개):\n"
                result_text += ", ".join(failed_names) + "\n\n"

            if not result_text:
                result_text = "새로 업데이트된 저장소가 없습니다.\n(모두 이미 최신 상태이거나 실패함)"

            if failed_names:
                messagebox.showwarning("업데이트 결과 (실패 포함)", result_text.strip())
            else:
                messagebox.showinfo("업데이트 완료", result_text.strip())

        self.root.after(0, show_result_popup)

        self.update_result_tab(updated_names, failed_names)

    def update_result_tab(self, updated_names, failed_names):
        """상세 결과 탭 업데이트"""
        def _update():
            self.result_text.config(state=tk.NORMAL)
            self.result_text.delete(1.0, tk.END)

            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.result_text.insert(tk.END, f"📊 업데이트 결과 - {timestamp}\n")
            self.result_text.insert(tk.END, "=" * 50 + "\n\n")

            if updated_names:
                self.result_text.insert(tk.END, f"✅ 업데이트 성공 ({len(updated_names)}개)\n")
                self.result_text.insert(tk.END, "-" * 30 + "\n")
                for name in updated_names:
                    self.result_text.insert(tk.END, f"  ✓ {name}\n")
                self.result_text.insert(tk.END, "\n")

            if failed_names:
                self.result_text.insert(tk.END, f"❌ 업데이트 실패 ({len(failed_names)}개)\n")
                self.result_text.insert(tk.END, "-" * 30 + "\n")
                for name in failed_names:
                    self.result_text.insert(tk.END, f"  ✗ {name}\n")
                self.result_text.insert(tk.END, "\n")

            if not updated_names and not failed_names:
                self.result_text.insert(tk.END, "업데이트된 저장소가 없습니다.\n")

            self.result_text.config(state=tk.DISABLED)

        self.root.after(0, _update)

    def open_log_file(self):
        """로그 파일 열기"""
        if not os.path.exists(Config.LOG_FILE):
            messagebox.showinfo("알림", "로그 파일이 없습니다.")
            return

        try:
            if os.name == 'nt':
                os.startfile(Config.LOG_FILE)
            else:
                import subprocess
                opener = 'open' if platform.system() == 'Darwin' else 'xdg-open'
                subprocess.call([opener, Config.LOG_FILE])
        except Exception as e:
            messagebox.showerror("오류", f"로그 파일을 열 수 없습니다:\n{str(e)}")


# ==============================================================================
# 메인 GUI 애플리케이션 (GitUpdaterGUI)
# ==============================================================================

class GitUpdaterGUI(LayoutMixin, QueueMixin, RepoMixin, UpdaterMixin):
    """Git 저장소 자동 업데이트 GUI"""

    def __init__(self, root):
        self.root = root

        os.environ['GIT_TERMINAL_PROMPT'] = '0'
        os.environ['GIT_SSH_COMMAND'] = 'ssh -o BatchMode=yes'

        self.config = Config()

        self.root.title("🔄 Git Branch Auto Updater")
        geometry = self.config.window_size
        if self.config.window_position:
            pos = self.config.window_position
            if not pos.startswith('+'):
                pos = f'+{pos}'
            geometry = f"{self.config.window_size}{pos}"
        self.root.geometry(geometry)
        self.root.minsize(1000, 600)

        self.repo_paths = {}
        self.repo_status = {}
        self.repo_branches = {}
        self.is_updating = False
        self.is_loading = False
        self.log_entries = []
        self.current_log_filter = 'ALL'

        self.ui_queue = queue.Queue()
        self.log_lock = threading.Lock()

        self.stash_option_var = tk.BooleanVar(value=self.config.stash_option)
        self.batch_size_var = tk.IntVar(value=self.config.batch_size)
        self.search_var = tk.StringVar()

        self.setup_styles()
        self.create_widgets()
        self.bind_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.process_queue()
        self.root.after(100, self.load_paths_async)

        self.log_message("프로그램 시작", level="INFO")
        self.log_separator()

    def bind_shortcuts(self):
        """단축키 바인딩"""
        self.root.bind('<Control-a>', lambda e: self.select_all())
        self.root.bind('<Control-A>', lambda e: self.select_all())
        self.root.bind('<Delete>', lambda e: self.remove_repository())
        self.root.bind('<F5>', lambda e: self.refresh_repo_list_async())

    def on_closing(self):
        """앱 종료 시 처리"""
        try:
            geo = self.root.geometry()
            parts = geo.split('+')
            self.config.window_size = parts[0]
            if len(parts) >= 3:
                self.config.window_position = f"{parts[1]}+{parts[2]}"
            elif len(parts) == 2:
                self.config.window_position = parts[1]
        except Exception:
            pass
        self.config.save()

        self.root.destroy()


# ==============================================================================
# 메인 진입점 (main)
# ==============================================================================

def main():
    root = tk.Tk()
    GitUpdaterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()