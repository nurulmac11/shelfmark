import { useCallback, useState } from 'react';

import { useMountEffect } from '../hooks/useMountEffect';
import type { ForYouRow } from '../services/api';
import { getForYou } from '../services/api';
import type { Book, ButtonStateInfo } from '../types';
import { CardView } from './resultsViews/CardView';

interface ForYouSectionProps {
  onDetails: (id: string) => Promise<void>;
  onDownload: (book: Book) => Promise<void>;
  onGetReleases: (book: Book) => Promise<void>;
  getButtonState: (bookId: string) => ButtonStateInfo;
  /** Called with every book shown, so the details modal can find them. */
  onBooksLoaded: (books: Book[]) => void;
  onClose: () => void;
  onShowToast?: (message: string, type: 'success' | 'error' | 'info') => void;
}

export const ForYouSection = ({
  onDetails,
  onDownload,
  onGetReleases,
  getButtonState,
  onBooksLoaded,
  onClose,
  onShowToast,
}: ForYouSectionProps) => {
  const [rows, setRows] = useState<ForYouRow[]>([]);
  const [seedCount, setSeedCount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Only sets state after the request resolves, so it is safe to run from an effect.
  const fetchRows = useCallback(
    async (refresh: boolean) => {
      try {
        const result = await getForYou(refresh);
        setRows(result.rows);
        setSeedCount(result.seedCount);
        setError(null);
        onBooksLoaded(result.rows.flatMap((row) => row.books));
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not load recommendations');
      } finally {
        setIsLoading(false);
      }
    },
    [onBooksLoaded],
  );

  useMountEffect(() => {
    void fetchRows(false);
  });

  const handleRefresh = () => {
    setIsLoading(true);
    setError(null);
    void fetchRows(true);
  };

  return (
    <section id="for-you-section" className="mb-4 w-full sm:mb-8">
      <div className="mb-4 flex items-center gap-3">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">For You</h2>
        {!isLoading && seedCount > 0 && (
          <span className="text-xs text-gray-500 dark:text-gray-400">
            Based on {seedCount} {seedCount === 1 ? 'book' : 'books'} in your library
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={handleRefresh}
            disabled={isLoading}
            className="hover-action rounded-full px-3 py-1.5 text-sm text-gray-700 transition-colors disabled:opacity-50 dark:text-gray-300"
            title="Rebuild recommendations"
          >
            Refresh
          </button>
          <button
            type="button"
            onClick={onClose}
            className="hover-action rounded-full px-3 py-1.5 text-sm text-gray-700 transition-colors dark:text-gray-300"
          >
            Back to search
          </button>
        </div>
      </div>

      {isLoading && (
        <p className="py-10 text-center text-sm text-gray-500 dark:text-gray-400">
          Finding books for you… the first load can take a few seconds.
        </p>
      )}

      {!isLoading && error && (
        <p className="py-10 text-center text-sm text-red-600 dark:text-red-400">{error}</p>
      )}

      {!isLoading && !error && rows.length === 0 && (
        <p className="py-10 text-center text-sm text-gray-500 dark:text-gray-400">
          No recommendations yet — download a few books and they will appear here.
        </p>
      )}

      {!isLoading &&
        !error &&
        rows.map((row) => (
          <div key={row.key} className="mb-8">
            <h3 className="mb-3 text-base font-medium text-gray-800 dark:text-gray-200">
              {row.title}
            </h3>
            <div className="-mx-1 flex snap-x gap-5 overflow-x-auto px-1 pb-3">
              {row.books.map((book, index) => (
                <div key={book.id} className="w-40 shrink-0 snap-start sm:w-48">
                  <CardView
                    book={book}
                    onDetails={onDetails}
                    onDownload={onDownload}
                    onGetReleases={onGetReleases}
                    buttonState={getButtonState(book.id)}
                    animationDelay={Math.min(index, 8) * 40}
                    onShowToast={onShowToast}
                  />
                </div>
              ))}
            </div>
          </div>
        ))}
    </section>
  );
};
