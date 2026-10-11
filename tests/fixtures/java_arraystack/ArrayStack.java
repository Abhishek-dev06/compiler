@Deprecated
public class ArrayStack<T> {
    private final T value;

    public ArrayStack(T value) {
        this.value = value;
    }

    public T peek() {
        return value;
    }
}
